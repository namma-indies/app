"""One animal's own page, and what it may say about other people's sightings."""
import pytest

from app.ids import uuid7
from app.security import issue_session
from tests.test_dogs_route import _make_individual, _pool, _post, _second_observer


async def _sql(client, query, *args):
    async with (await _pool(client)).acquire() as c:
        return await c.fetch(query, *args)


@pytest.mark.asyncio
async def test_requires_auth(app_client):
    assert (await app_client.get(f"/dogs/{uuid7()}")).status_code == 401


@pytest.mark.asyncio
async def test_the_page_is_the_card_plus_every_sighting_newest_first(authed_client):
    client, _ = authed_client
    old = await _post(client, when="2026-03-03T07:40:00Z")
    new = await _post(client, when="2026-11-12T12:15:00Z")
    iid = await _make_individual(client, [old, new])
    await client.get(f"/dogs/{iid}")  # queues the cell for a name
    await _sql(client, "UPDATE place_labels SET label='Indiranagar, Bengaluru'")

    page = (await client.get(f"/dogs/{iid}")).json()
    card = next(d for d in (await client.get("/dogs")).json()["dogs"] if d["id"] == str(iid))
    # Photo URLs are signed per request, so they are compared by count.
    assert {k: page[k] for k in card if k != "photos"} == {k: v for k, v in card.items() if k != "photos"}, \
        "the page opens with exactly the card"
    assert len(page["photos"]) == len(card["photos"]) == 2
    assert [s["id"] for s in page["sightings"]] == [new, old]
    first = page["sightings"][0]
    assert first["mine"] is True and first["observer"] is None
    assert first["captured_at"].startswith("2026-11-12T12:15")
    assert first["day"] == "2026-11-12"
    assert first["place"] == "Indiranagar, Bengaluru"
    assert first["thumb_url"]


@pytest.mark.asyncio
async def test_someone_elses_sighting_gives_the_day_and_nothing_finer(authed_client):
    """Every sighting of one dog with its time and place is a schedule."""
    client, me = authed_client
    priya = await _second_observer(client, "Priya")
    client.cookies.set("session", issue_session(priya))
    theirs = await _post(client, when="2026-10-02T04:00:00Z")
    client.cookies.set("session", issue_session(me))
    mine = await _post(client, when="2026-11-20T13:05:00Z")
    iid = await _make_individual(client, [theirs, mine])
    await client.get(f"/dogs/{iid}")
    await _sql(client, "UPDATE place_labels SET label='Indiranagar, Bengaluru'")

    sightings = (await client.get(f"/dogs/{iid}")).json()["sightings"]
    other = next(s for s in sightings if not s["mine"])
    assert other == {"id": None, "mine": False, "observer": "Priya", "captured_at": None,
                     "day": "2026-10-02", "place": None, "thumb_url": other["thumb_url"]}
    assert other["thumb_url"]
    assert "lat" not in str(sightings) and "lng" not in str(sightings)


@pytest.mark.asyncio
async def test_an_animal_you_cannot_see_is_not_found(authed_client):
    client, me = authed_client
    assert (await client.get(f"/dogs/{uuid7()}")).status_code == 404

    priya = await _second_observer(client, "Priya")
    client.cookies.set("session", issue_session(priya))
    theirs = [await _post(client) for _ in range(2)]
    client.cookies.set("session", issue_session(me))
    iid = await _make_individual(client, theirs)
    assert (await client.get(f"/dogs/{iid}")).status_code == 200

    await _sql(client, "INSERT INTO observer_blocks (id, blocker_id, blocked_id) VALUES ($1,$2,$3)", uuid7(), me, priya)
    assert (await client.get(f"/dogs/{iid}")).status_code == 404


@pytest.mark.asyncio
async def test_a_merged_away_record_has_no_page(authed_client):
    client, _ = authed_client
    survivor = await _make_individual(client, [await _post(client)])
    loser = await _make_individual(client, [await _post(client)], merged_into=survivor, status="merged")
    assert (await client.get(f"/dogs/{loser}")).status_code == 404


@pytest.mark.asyncio
async def test_a_hidden_sighting_is_not_listed(authed_client):
    client, _ = authed_client
    a, b, c = await _post(client), await _post(client), await _post(client)
    iid = await _make_individual(client, [a, b, c])
    await _sql(client, "UPDATE sightings SET review_status='pending' WHERE id=$1::uuid", c)
    page = (await client.get(f"/dogs/{iid}")).json()
    assert {s["id"] for s in page["sightings"]} == {a, b}
    assert page["sighting_count"] == 2
