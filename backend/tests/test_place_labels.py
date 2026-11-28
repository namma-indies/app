"""Where a sighting was, in words, without saying more than the map does."""
import pytest

from app import place_labels
from app.config import settings
from app.place_labels import cell_of, fill_one, label_from, labels_for
from tests.test_dogs_route import _make_individual, _pool, _post, _second_observer

INDIRANAGAR = (12.9784, 77.6408)


def test_label_prefers_the_suburb_and_adds_the_city():
    # A real reply for a cell in Indiranagar (Nominatim, 28 Nov 2026).
    assert label_from({"address": {
        "road": "Chinmaya Mission Hospital Road", "neighbourhood": "Old Binnamangala", "suburb": "Indiranagar",
        "city_district": "Bengaluru Central City Corporation", "city": "Bengaluru", "county": "Bangalore North",
        "state_district": "Bengaluru Urban", "state": "Karnataka", "postcode": "560038", "country": "India",
    }}) == "Indiranagar, Bengaluru"
    assert label_from({"address": {"neighbourhood": "Fontainhas", "city": "Panaji"}}) == "Fontainhas, Panaji"
    # Out in the country, a village alone; never "Belagavi, Belagavi".
    assert label_from({"address": {"village": "Kakati", "county": "Belagavi"}}) == "Kakati, Belagavi"
    assert label_from({"address": {"city": "Panaji"}}) == "Panaji"
    assert label_from({"address": {"town": "Mapusa", "city": "Mapusa"}}) == "Mapusa"
    assert label_from({"error": "Unable to geocode"}) is None
    assert label_from({"address": {"road": "100 Feet Road"}}) is None
    assert len(label_from({"address": {"suburb": "x" * 300, "city": "y"}})) == 120


def test_every_point_in_a_cell_shares_one_name():
    a = cell_of(*INDIRANAGAR)
    b = cell_of(INDIRANAGAR[0] + 0.0004, INDIRANAGAR[1] - 0.0003)
    far = cell_of(INDIRANAGAR[0] + 0.02, INDIRANAGAR[1])
    assert a == b and a != far
    assert a[0] == int(settings.map_coarsen_cell_m)


@pytest.mark.asyncio
async def test_a_new_cell_is_queued_and_named_by_the_worker(migrated_db, monkeypatch):
    conn = migrated_db
    asked = []

    async def fake(lat, lng):
        asked.append((lat, lng))
        return "Indiranagar, Bengaluru"

    monkeypatch.setattr(place_labels, "_reverse", fake)
    monkeypatch.setattr("app.routes.places._last_request", 0.0)

    near = (INDIRANAGAR[0] + 0.0004, INDIRANAGAR[1])
    assert await labels_for(conn, [INDIRANAGAR, near, (None, None)]) == {}
    assert await conn.fetchval("SELECT count(*) FROM place_labels") == 1, "one row per cell"

    assert await fill_one(conn) is True
    assert await fill_one(conn) is False, "nothing left to do"
    # The provider was asked about the cell centre, never the point itself.
    assert asked == [cell_of(*INDIRANAGAR)[1:]]

    assert await labels_for(conn, [INDIRANAGAR, near]) == {
        INDIRANAGAR: "Indiranagar, Bengaluru", near: "Indiranagar, Bengaluru"}


@pytest.mark.asyncio
async def test_a_failed_lookup_waits_a_day_before_trying_again(migrated_db, monkeypatch):
    import httpx
    conn = migrated_db

    async def down(lat, lng):
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(place_labels, "_reverse", down)
    monkeypatch.setattr("app.routes.places._last_request", 0.0)
    await labels_for(conn, [INDIRANAGAR])
    assert await fill_one(conn) is True
    assert await fill_one(conn) is False
    assert await labels_for(conn, [INDIRANAGAR]) == {}

    await conn.execute("UPDATE place_labels SET looked_up_at = now() - interval '25 hours'")
    assert await fill_one(conn) is True


@pytest.mark.asyncio
async def test_switched_off_it_queues_nothing(migrated_db, monkeypatch):
    monkeypatch.setattr(settings, "place_labels_enabled", False)
    assert await labels_for(migrated_db, [INDIRANAGAR]) == {}
    assert await migrated_db.fetchval("SELECT count(*) FROM place_labels") == 0


async def _name_every_cell(client, label="Indiranagar, Bengaluru"):
    async with (await _pool(client)).acquire() as c:
        await c.execute("UPDATE place_labels SET label=$1, looked_up_at=now()", label)


@pytest.mark.asyncio
async def test_the_journal_and_a_dogs_card_say_where_in_words(authed_client):
    client, _ = authed_client
    a = await _post(client, lat=INDIRANAGAR[0], lng=INDIRANAGAR[1])
    b = await _post(client, lat=INDIRANAGAR[0], lng=INDIRANAGAR[1])
    await _make_individual(client, [a, b])

    # First look: queued, not yet named.
    assert all(s["place"] is None for s in (await client.get("/dex")).json()["sightings"])
    assert (await client.get("/dogs")).json()["dogs"][0]["place"] is None

    await _name_every_cell(client)
    assert {s["place"] for s in (await client.get("/dex")).json()["sightings"]} == {"Indiranagar, Bengaluru"}
    assert (await client.get("/dogs")).json()["dogs"][0]["place"] == "Indiranagar, Bengaluru"


@pytest.mark.asyncio
async def test_someone_elses_dog_is_named_by_the_cell_they_were_already_shown(authed_client):
    from app.security import issue_session
    client, me = authed_client
    other = await _second_observer(client, "Priya")
    client.cookies.set("session", issue_session(other))
    theirs = [await _post(client, lat=INDIRANAGAR[0], lng=INDIRANAGAR[1]) for _ in range(2)]
    client.cookies.set("session", issue_session(me))
    await _make_individual(client, theirs)
    await client.get("/dogs")
    await _name_every_cell(client)

    dog = (await client.get("/dogs")).json()["dogs"][0]
    assert dog["precision"] == "area"
    assert dog["place"] == "Indiranagar, Bengaluru"
    # Same cell as the coordinate the card already gives this viewer.
    assert cell_of(dog["lat"], dog["lng"]) == cell_of(*INDIRANAGAR)
