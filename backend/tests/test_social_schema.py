"""The post queue's guarantees, held by the schema rather than by code.

Every rule here is one the daily run must never break -- a second post on the
same day, the same sighting or photo twice -- so each is a constraint the
database refuses, not a check a future change could forget to make.
"""

from datetime import date

import asyncpg
import pytest

from app.ids import uuid7


async def _observer(conn):
    oid = uuid7()
    await conn.execute(
        "INSERT INTO observers (id, display_name, created_via) VALUES ($1,'T','test')", oid)
    return oid


async def _sighting_with_photo(conn, oid):
    sid, pid = uuid7(), uuid7()
    await conn.execute(
        "INSERT INTO sightings (id, observer_id, captured_at, geo_source) "
        "VALUES ($1,$2,now(),'none')", sid, oid)
    await conn.execute(
        "INSERT INTO photos (id, sighting_id, s3_key) VALUES ($1,$2,'k')", pid, sid)
    return sid, pid


async def _pick(conn, sid, pid, *, status="queued", caption="Spotted", posted_on=None):
    await conn.execute(
        "INSERT INTO social_picks (id, sighting_id, photo_id, caption, status, posted_on) "
        "VALUES ($1,$2,$3,$4,$5,$6)", uuid7(), sid, pid, caption, status, posted_on)


async def test_one_pick_per_sighting(migrated_db):
    oid = await _observer(migrated_db)
    sid, pid = await _sighting_with_photo(migrated_db, oid)
    await _pick(migrated_db, sid, pid)
    with pytest.raises(asyncpg.UniqueViolationError):
        await _pick(migrated_db, sid, None, status="passed", caption=None)


async def test_one_pick_per_photo(migrated_db):
    oid = await _observer(migrated_db)
    sid, pid = await _sighting_with_photo(migrated_db, oid)
    other, _ = await _sighting_with_photo(migrated_db, oid)
    await _pick(migrated_db, sid, pid)
    with pytest.raises(asyncpg.UniqueViolationError):
        await _pick(migrated_db, other, pid)


async def test_one_post_per_day(migrated_db):
    oid = await _observer(migrated_db)
    a, pa = await _sighting_with_photo(migrated_db, oid)
    b, pb = await _sighting_with_photo(migrated_db, oid)
    day = date(2026, 10, 25)
    await _pick(migrated_db, a, pa, status="posted", posted_on=day)
    with pytest.raises(asyncpg.UniqueViolationError):
        await _pick(migrated_db, b, pb, status="posted", posted_on=day)


async def test_queued_needs_photo_and_caption(migrated_db):
    oid = await _observer(migrated_db)
    sid, pid = await _sighting_with_photo(migrated_db, oid)
    with pytest.raises(asyncpg.CheckViolationError):
        await _pick(migrated_db, sid, pid, caption=None)
    await _pick(migrated_db, sid, None, status="passed", caption=None)


async def test_consent_defaults_false(migrated_db):
    oid = await _observer(migrated_db)
    row = await migrated_db.fetchrow(
        "SELECT social_consent, x_handle FROM observers WHERE id=$1", oid)
    assert row["social_consent"] is False
    assert row["x_handle"] is None
