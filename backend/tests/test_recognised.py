"""A dog keeps their number, and you are told when a sighting of yours is one.

Three things, and they lean on each other: `individuals.number` has to be
stable before anything can be said about "No. 047"; `sightings.identified_at`
has to be stamped by the database, whoever makes the link; and `/recognised`
has to stay inside what `/dogs` already shows this viewer.
"""
import os
import subprocess
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from app.ids import uuid7
from app.security import issue_session
from tests.test_dogs_route import _make_individual, _pool, _post, _second_observer
from tests.test_launch_migrations import scratch_db  # noqa: F401  (fixture)
from tests.test_merge_authorization import _proposal


async def _as(client, oid):
    client.cookies.set("session", issue_session(oid))


async def _same(client, a, b):
    """Confirm a pair the way a person does, through the real verdict route."""
    r = await client.post(f"/proposal/{await _proposal(client, a, b)}", data={"verdict": "same"})
    assert r.status_code == 200
    return r.json()


async def _recognised(client):
    r = await client.get("/recognised")
    assert r.status_code == 200
    return r.json()


async def _sql(client, query, *args):
    async with (await _pool(client)).acquire() as c:
        return await c.fetch(query, *args)


# --- the number ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_dog_keeps_their_number_when_another_is_seen_again(authed_client):
    """The card's number used to be its position in a list sorted by last-seen,
    so seeing any dog again renumbered every dog above them."""
    client, _ = authed_client
    first = await _make_individual(client, [await _post(client, when="2026-07-01T09:00:00Z")])
    second = await _make_individual(client, [await _post(client, when="2026-07-02T09:00:00Z")])

    def numbers(dogs):
        return {d["id"]: d["number"] for d in dogs}

    before = (await client.get("/dogs")).json()["dogs"]
    assert numbers(before) == {str(first): 1, str(second): 2}
    assert [d["id"] for d in before] == [str(second), str(first)], "newest sighting first"

    # The older dog is met again: they move to the top and keep being No. 1.
    await _make_individual(client, [])  # an unrelated animal takes No. 3
    again = await _post(client, when="2026-07-20T09:00:00Z")
    await _sql(client, "UPDATE sightings SET individual_id=$1, match_status='confirmed' "
                       "WHERE id=$2::uuid", first, again)

    after = (await client.get("/dogs")).json()["dogs"]
    assert [d["id"] for d in after] == [str(first), str(second)]
    assert numbers(after) == {str(first): 1, str(second): 2}


@pytest.mark.asyncio
async def test_a_verdict_says_who_it_made(authed_client):
    client, _ = authed_client
    a, b = await _post(client), await _post(client)
    made = await _same(client, a, b)
    assert made["number"] == 1
    assert (await client.get("/dogs")).json()["dogs"][0]["number"] == 1

    c, d = await _post(client), await _post(client)
    r = await client.post(f"/proposal/{await _proposal(client, c, d)}", data={"verdict": "different"})
    assert r.json()["number"] is None


@pytest.mark.asyncio
async def test_the_journal_says_who_a_sighting_turned_out_to_be(authed_client):
    client, _ = authed_client
    a = await _post(client, when="2026-07-01T09:00:00Z")
    b = await _post(client, when="2026-07-02T09:00:00Z")
    unknown = await _post(client, when="2026-07-03T09:00:00Z")
    made = await _same(client, a, b)

    by_id = {s["id"]: s for s in (await client.get("/dex")).json()["sightings"]}
    assert by_id[a]["individual"] == {"id": made["individual_id"], "number": 1, "name": None}
    assert by_id[b]["individual"]["number"] == 1
    assert by_id[unknown]["individual"] is None


@pytest.mark.asyncio
async def test_a_merged_away_record_is_not_an_identity(authed_client):
    client, _ = authed_client
    s = await _post(client)
    survivor = await _make_individual(client, [await _post(client)])
    await _make_individual(client, [s], merged_into=survivor, status="merged")
    by_id = {x["id"]: x for x in (await client.get("/dex")).json()["sightings"]}
    assert by_id[s]["individual"] is None


# --- when a sighting was recognised ---------------------------------------------


@pytest.mark.asyncio
async def test_the_database_dates_a_link_whoever_makes_it(authed_client):
    client, _ = authed_client
    a = await _post(client)

    async def stamp():
        return (await _sql(client, "SELECT identified_at FROM sightings WHERE id=$1::uuid", a))[0][0]

    assert await stamp() is None
    iid = await _make_individual(client, [a])
    first = await stamp()
    assert first is not None

    # Saying the same thing again is not being recognised again.
    await _sql(client, "UPDATE sightings SET individual_id=$1 WHERE id=$2::uuid", iid, a)
    assert await stamp() == first

    await _sql(client, "UPDATE sightings SET individual_id=NULL WHERE id=$1::uuid", a)
    assert await stamp() is None


# --- the moment -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_requires_auth(app_client):
    assert (await app_client.get("/recognised")).status_code == 401
    assert (await app_client.post("/recognised/seen", data={"as_of": "2026-11-27T00:00:00Z"})).status_code == 401


@pytest.mark.asyncio
async def test_nothing_to_tell_before_anything_is_recognised(authed_client):
    client, _ = authed_client
    await _post(client)
    got = await _recognised(client)
    assert got["recognised"] == [] and got["more"] == 0


@pytest.mark.asyncio
async def test_two_of_your_own_sightings_become_one_dog(authed_client):
    client, _ = authed_client
    march = await _post(client, when="2026-03-03T09:00:00Z")
    middle = await _post(client, when="2026-07-01T09:00:00Z")
    november = await _post(client, when="2026-11-12T09:00:00Z")
    await _same(client, middle, march)
    await _same(client, november, middle)

    cards = (await _recognised(client))["recognised"]
    assert len(cards) == 1, "one card per animal, not per sighting"
    card = cards[0]
    assert card["individual"]["number"] == 1
    assert card["individual"]["species"] is None or card["individual"]["species"] in ("dog", "cat")
    # The latest sighting of yours, beside the one furthest from it in time.
    assert card["yours"]["id"] == november
    assert card["other"]["captured_at"].startswith("2026-03-03")
    assert card["other"]["mine"] is True and card["other"]["observer"] is None
    assert card["met_count"] == 3 and card["sighting_count"] == 3
    assert card["observers"] == []
    assert card["yours"]["thumb_url"] and card["other"]["thumb_url"]
    # Nothing here says where anyone was.
    assert "lat" not in str(card) and "lng" not in str(card)


@pytest.mark.asyncio
async def test_someone_else_had_met_them_first(authed_client):
    client, me = authed_client
    priya = await _second_observer(client, "Priya")
    await _as(client, priya)
    theirs = await _post(client, when="2026-03-03T09:00:00Z")
    await _as(client, me)
    mine = await _post(client, when="2026-11-12T09:00:00Z")
    await _same(client, mine, theirs)

    card = (await _recognised(client))["recognised"][0]
    assert card["yours"]["id"] == mine
    assert card["other"]["mine"] is False and card["other"]["observer"] == "Priya"
    assert card["met_count"] == 1 and card["observers"] == ["Priya"]

    # Priya is told about hers, with mine beside it.
    await _as(client, priya)
    hers = (await _recognised(client))["recognised"][0]
    assert hers["yours"]["id"] == theirs and hers["other"]["observer"] == "Tester"


@pytest.mark.asyncio
async def test_someone_else_meeting_a_dog_you_know_is_not_announced(authed_client):
    """That would be a statement about where another person was and when."""
    client, me = authed_client
    a, b = await _post(client), await _post(client)
    made = await _same(client, a, b)
    got = await _recognised(client)
    await client.post("/recognised/seen", data={"as_of": got["as_of"]})

    other = await _second_observer(client, "Priya")
    await _as(client, other)
    theirs = await _post(client, when="2026-11-20T09:00:00Z")
    await _sql(client, "UPDATE sightings SET individual_id=$1::uuid, match_status='confirmed' "
                       "WHERE id=$2::uuid", made["individual_id"], theirs)
    await _as(client, me)
    assert (await _recognised(client))["recognised"] == []


@pytest.mark.asyncio
async def test_the_same_photo_twice_is_not_a_recognition(authed_client):
    """An identical upload mints an individual too (app/identical.py). The
    later copy is a duplicate, so there is no second photograph to show."""
    client, _ = authed_client
    a, b = await _post(client), await _post(client)
    await _make_individual(client, [a, b])
    await _sql(client, "UPDATE sightings SET duplicate_of=$1::uuid WHERE id=$2::uuid", a, b)
    assert (await _recognised(client))["recognised"] == []


@pytest.mark.asyncio
async def test_a_blocked_persons_sighting_is_not_the_other_photo(authed_client):
    client, me = authed_client
    other = await _second_observer(client, "Priya")
    await _as(client, other)
    theirs = await _post(client)
    await _as(client, me)
    mine = await _post(client)
    await _same(client, mine, theirs)
    await _sql(client, "INSERT INTO observer_blocks (id, blocker_id, blocked_id) VALUES ($1,$2,$3)",
               uuid7(), me, other)
    assert (await _recognised(client))["recognised"] == []


@pytest.mark.asyncio
async def test_seen_means_seen_until_the_next_one(authed_client):
    client, _ = authed_client
    a, b = await _post(client), await _post(client)
    await _same(client, a, b)
    got = await _recognised(client)
    assert len(got["recognised"]) == 1

    # Read up to the card itself, with its timestamp passed back untouched: a
    # client that rounded it to milliseconds would be shown the card forever.
    seen = await client.post("/recognised/seen", data={"as_of": got["recognised"][0]["recognised_at"]})
    assert seen.status_code == 204
    assert (await _recognised(client))["recognised"] == []

    # A third sighting of the same dog is a new thing to tell.
    c = await _post(client, when="2026-12-01T09:00:00Z")
    await _same(client, c, a)
    again = (await _recognised(client))["recognised"]
    assert [card["yours"]["id"] for card in again] == [c]
    assert again[0]["met_count"] == 3


@pytest.mark.asyncio
async def test_seen_only_moves_forward_and_never_past_now(authed_client):
    client, me = authed_client

    async def mark():
        return (await _sql(client, "SELECT recognitions_seen_at FROM observers WHERE id=$1", me))[0][0]

    await client.post("/recognised/seen", data={"as_of": "2026-06-01T00:00:00+00:00"})
    assert (await mark()).date().isoformat() == "2026-06-01"
    await client.post("/recognised/seen", data={"as_of": "2026-01-01T00:00:00+00:00"})
    assert (await mark()).date().isoformat() == "2026-06-01"
    await client.post("/recognised/seen", data={"as_of": "2999-01-01T00:00:00+00:00"})
    assert await mark() <= datetime.now(timezone.utc) + timedelta(seconds=5)


@pytest.mark.asyncio
async def test_a_first_look_shows_recent_ones_not_all_of_history(authed_client):
    client, _ = authed_client
    a, b = await _post(client), await _post(client)
    await _make_individual(client, [a, b])
    await _sql(client, "UPDATE sightings SET identified_at = now() - interval '90 days' "
                       "WHERE id = ANY($1::uuid[])", [a, b])
    assert (await _recognised(client))["recognised"] == []

    c, d = await _post(client), await _post(client)
    await _same(client, c, d)
    assert len((await _recognised(client))["recognised"]) == 1


@pytest.mark.asyncio
async def test_only_a_handful_are_shown_and_the_rest_are_counted(authed_client):
    from app.routes.recognised import MAX_SHOWN
    client, _ = authed_client
    for _ in range(MAX_SHOWN + 2):
        await _make_individual(client, [await _post(client), await _post(client)])
    got = await _recognised(client)
    assert len(got["recognised"]) == MAX_SHOWN and got["more"] == 2


# --- migration ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_0050_numbers_existing_dogs_and_dates_existing_links(scratch_db):
    from tests.conftest import BACKEND_ROOT
    url, upgrade = scratch_db
    upgrade("0049_multi_animal_grouping")
    conn = await asyncpg.connect(url)
    older, newer, merged, empty, obs = uuid7(), uuid7(), uuid7(), uuid7(), uuid7()
    linked, verdict_side, loose = uuid7(), uuid7(), uuid7()
    try:
        await conn.execute("INSERT INTO observers (id, display_name, created_via) VALUES ($1,'A','test')", obs)
        # Created newest-first on purpose: numbering follows created_at, not insertion.
        await conn.execute("""
            INSERT INTO individuals (id, created_by, status, merged_into, created_at) VALUES
              ($1,'feeder','active',NULL,'2026-09-02'), ($2,'feeder','active',NULL,'2026-09-01'),
              ($3,'feeder','active',NULL,'2026-08-01')""", newer, older, empty)
        await conn.execute("INSERT INTO individuals (id, created_by, status, merged_into, created_at) "
                           "VALUES ($1,'feeder','merged',$2,'2026-07-01')", merged, older)
        for sid, iid in ((linked, older), (verdict_side, newer), (loose, None)):
            await conn.execute(
                "INSERT INTO sightings (id, observer_id, captured_at, individual_id, updated_at) "
                "VALUES ($1,$2,'2026-09-01',$3,'2026-09-05')", sid, obs, iid)
        await conn.execute(
            "INSERT INTO confirmations (id, sighting_id, individual_id, observer_id, verdict, created_at) "
            "VALUES ($1,$2,$3,$4,'same','2026-10-10')", uuid7(), verdict_side, newer, obs)
    finally:
        await conn.close()

    upgrade("0050_recognised")
    conn = await asyncpg.connect(url)
    try:
        numbers = {r["id"]: r["number"] for r in await conn.fetch("SELECT id, number FROM individuals")}
        # Dogs with sightings first, in the order they were made; then the
        # empty one; the merged-away record last.
        assert numbers == {older: 1, newer: 2, empty: 3, merged: 4}
        fresh = uuid7()
        await conn.execute("INSERT INTO individuals (id, created_by) VALUES ($1,'feeder')", fresh)
        assert await conn.fetchval("SELECT number FROM individuals WHERE id=$1", fresh) == 5

        dated = {r["id"]: r["identified_at"] for r in await conn.fetch("SELECT id, identified_at FROM sightings")}
        assert dated[verdict_side].date().isoformat() == "2026-10-10", "from the verdict on record"
        assert dated[linked].date().isoformat() == "2026-09-05", "otherwise the row's last update"
        assert dated[loose] is None
    finally:
        await conn.close()

    env = os.environ.copy()
    env["ALEMBIC_DB_URL"] = url.replace("postgresql://", "postgresql+psycopg://")
    subprocess.run(["uv", "run", "alembic", "downgrade", "0049_multi_animal_grouping"], cwd=BACKEND_ROOT,
                   env=env, check=True, capture_output=True)
    conn = await asyncpg.connect(url)
    try:
        assert await conn.fetchval("""SELECT count(*) FROM information_schema.columns
            WHERE (table_name, column_name) IN (('individuals','number'), ('sightings','identified_at'),
                                                ('observers','recognitions_seen_at'))""") == 0
        assert await conn.fetchval("SELECT to_regclass('individuals_number_seq') IS NULL")
    finally:
        await conn.close()
