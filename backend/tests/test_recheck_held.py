"""Uploads held for review under the old rule are re-checked against today's.

PR #87 narrowed review to the one case that needs a person: two animals of a
species in the same frame that then reappear elsewhere. It changed what new
uploads do, and nothing went back. On 2026-10-24 one clip -- a single dog in
each of 48 frames -- had been waiting a month on a "separate the animals"
question with nothing to separate.

The re-check works from the instances already stored: species per frame is all
the rule reads, so no GPU pass is needed.
"""

import asyncpg
import pytest

from app.capture_jobs import complete_capture_job
from app.config import settings
from tests.test_multi_captures import seed, tracked


def held(*frames):
    """A completion as the pre-#87 worker sent it: every instance its own
    track, and the whole capture held for review."""
    body = tracked(*frames)
    instances = [i.model_copy(update={"track_id": f"old:{n}"}) for n, i in enumerate(body.instances)]
    return body.model_copy(update={"instances": instances, "needs_review": True})


async def _held_capture(conn, *frames):
    body = held(*frames)
    oid, cid, jid, storage = await seed(conn, body)
    assert (await complete_capture_job(conn, storage, jid, body))["outcome"] == "needs_review"
    return cid


async def _recheck(conn, cid):
    from app.captures import recheck_held
    from app.matching import lock_matching
    async with conn.transaction():
        await lock_matching(conn)
        capture = await conn.fetchrow("SELECT * FROM captures WHERE id=$1 FOR UPDATE", cid)
        return await recheck_held(conn, capture)


async def _published(conn, cid):
    return await conn.fetch(
        "SELECT s.id, s.species, (SELECT count(*) FROM animal_instances a WHERE a.sighting_id = s.id) AS n "
        "FROM sightings s WHERE s.capture_id = $1 ORDER BY s.species", cid)


@pytest.mark.asyncio
async def test_one_dog_in_every_frame_publishes_as_one_dog(migrated_db):
    """The production case: nothing was ever ambiguous."""
    conn = migrated_db
    cid = await _held_capture(conn, ("dog",), ("dog",), ("dog",), ("dog",))
    sids = await _recheck(conn, cid)
    assert len(sids) == 1
    rows = await _published(conn, cid)
    assert [(r["species"], r["n"]) for r in rows] == [("dog", 4)]
    state = await conn.fetchval("SELECT processing_state FROM captures WHERE id=$1", cid)
    assert state == "ready"


@pytest.mark.asyncio
async def test_two_dogs_together_that_reappear_still_wait_for_a_person(migrated_db):
    conn = migrated_db
    cid = await _held_capture(conn, ("dog", "dog"), ("dog",))
    assert await _recheck(conn, cid) is None
    assert await _published(conn, cid) == []
    assert await conn.fetchval("SELECT processing_state FROM captures WHERE id=$1", cid) == "needs_review"


@pytest.mark.asyncio
async def test_a_dog_and_a_cat_publish_as_two_animals(migrated_db):
    conn = migrated_db
    cid = await _held_capture(conn, ("dog", "cat"), ("dog",), ("cat",))
    await _recheck(conn, cid)
    assert [(r["species"], r["n"]) for r in await _published(conn, cid)] == [("cat", 2), ("dog", 2)]


@pytest.mark.asyncio
async def test_a_draft_someone_saved_is_left_alone(migrated_db):
    """A person who started sorting has said something the rule cannot see."""
    conn = migrated_db
    cid = await _held_capture(conn, ("dog",), ("dog",))
    ids = [r["id"] for r in await conn.fetch("SELECT id FROM animal_instances WHERE capture_id=$1", cid)]
    await conn.execute(
        "UPDATE captures SET review_groups=$2::jsonb WHERE id=$1", cid,
        '[{"instance_ids":["%s"]},{"instance_ids":["%s"]}]' % (ids[0], ids[1]))
    assert await _recheck(conn, cid) is None
    assert await _published(conn, cid) == []


@pytest.mark.asyncio
async def test_script_dry_run_writes_nothing_and_apply_publishes(migrated_db):
    from scripts.recheck_held_captures import run
    conn = migrated_db
    single = await _held_capture(conn, ("dog",), ("dog",))
    pair = await _held_capture(conn, ("dog", "dog"), ("dog",))
    pool = await asyncpg.create_pool(settings.test_database_url, min_size=1, max_size=2)
    try:
        dry = await run(pool, apply=False)
        assert (dry.held, dry.published, dry.still_held) == (2, 1, 1)
        assert await _published(conn, single) == []
        real = await run(pool, apply=True)
        assert (real.held, real.published, real.still_held) == (2, 1, 1)
    finally:
        await pool.close()
    assert len(await _published(conn, single)) == 1
    assert await _published(conn, pair) == []


@pytest.mark.asyncio
async def test_two_dogs_in_one_photo_publish_as_two_dogs(migrated_db):
    """Co-visible but never reappearing: two animals by construction, so each
    box is its own dog -- not one group, which publication would refuse."""
    conn = migrated_db
    cid = await _held_capture(conn, ("dog", "dog"))
    sids = await _recheck(conn, cid)
    assert len(sids) == 2
    assert [(r["species"], r["n"]) for r in await _published(conn, cid)] == [("dog", 1), ("dog", 1)]


@pytest.mark.asyncio
async def test_two_dogs_together_and_a_cat_elsewhere_publish_three_animals(migrated_db):
    conn = migrated_db
    cid = await _held_capture(conn, ("dog", "dog"), ("cat",), ("cat",))
    await _recheck(conn, cid)
    assert sorted((r["species"], r["n"]) for r in await _published(conn, cid)) == [
        ("cat", 2), ("dog", 1), ("dog", 1)]


@pytest.mark.asyncio
async def test_script_carries_on_past_a_capture_it_cannot_publish(migrated_db, monkeypatch):
    import app.captures as captures
    from scripts.recheck_held_captures import run
    conn = migrated_db
    bad = await _held_capture(conn, ("dog",), ("dog",))
    good = await _held_capture(conn, ("dog",), ("dog",))
    real = captures.recheck_held

    async def flaky(c, capture):
        if capture["id"] == bad:
            raise RuntimeError("boom")
        return await real(c, capture)
    monkeypatch.setattr(captures, "recheck_held", flaky)
    pool = await asyncpg.create_pool(settings.test_database_url, min_size=1, max_size=2)
    try:
        rep = await run(pool, apply=True)
    finally:
        await pool.close()
    assert (rep.published, rep.failed) == (1, 1)
    assert len(await _published(conn, good)) == 1
