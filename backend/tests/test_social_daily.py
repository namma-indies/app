"""The daily run: one post a day at most, and never the same thing twice.

Every test here is one of the failures the design calls a failure even if the
code "works": a second post on a day the timer fired twice, a repost after X
accepted a post whose answer was lost, or posting something moderation has
since taken down.
"""

import io
from datetime import date, timedelta

import asyncpg
import pytest
import pytest_asyncio
from PIL import Image

from app.config import settings
from app.ids import uuid7
from app.social import run_daily
from app.x_client import XRejected, XUncertain
from tests import social_fixtures as fx

DAY = date(2026, 10, 25)


class FakeStorage:
    def __init__(self):
        self.reads = []

    async def get(self, key):
        self.reads.append(key)
        b = io.BytesIO()
        Image.new("RGB", (640, 480), (90, 120, 80)).save(b, "WEBP")
        return b.getvalue()


class FakeX:
    def __init__(self, *, upload_error=None, create_error=None):
        self.calls = []
        self.upload_error, self.create_error = upload_error, create_error

    async def upload_image(self, jpeg):
        self.calls.append(("upload", len(jpeg)))
        if self.upload_error:
            raise self.upload_error
        return f"m{len(self.calls)}"

    async def create_post(self, text, media_id):
        self.calls.append(("post", text, media_id))
        if self.create_error:
            raise self.create_error
        return f"p{len(self.calls)}"


@pytest_asyncio.fixture
async def pool(migrated_db):
    p = await asyncpg.create_pool(settings.test_database_url)
    yield p
    await p.close()


async def _queued(conn, *, caption="Spotted in Indiranagar 📸 @a_handle", picked_offset=0, **kw):
    oid = await fx.observer(conn)
    sid, pids = await fx.legacy_sighting(conn, oid, **kw)
    pick_id = uuid7()
    await conn.execute(
        "INSERT INTO social_picks (id, sighting_id, photo_id, caption, status, picked_at) "
        "VALUES ($1,$2,$3,$4,'queued', now() + make_interval(secs => $5))",
        pick_id, sid, pids[0], caption, picked_offset)
    return pick_id, sid, oid


async def _row(conn, pick_id):
    return await conn.fetchrow("SELECT * FROM social_picks WHERE id=$1", pick_id)


async def test_dry_run_calls_nothing_and_changes_nothing(migrated_db, pool):
    pick_id, _, _ = await _queued(migrated_db)
    x, storage = FakeX(), FakeStorage()
    result = await run_daily(pool, storage, x, today=DAY, enabled=False)
    assert result.outcome == "dry_run" and result.pick_id == pick_id
    assert "Indiranagar" in result.detail
    assert x.calls == [] and storage.reads == []
    assert (await _row(migrated_db, pick_id))["status"] == "queued"


async def test_posts_oldest_queued_and_records_day(migrated_db, pool):
    newer, _, _ = await _queued(migrated_db, picked_offset=60, caption="Spotted in B 📸 @a")
    older, _, _ = await _queued(migrated_db, picked_offset=0, caption="Spotted in A 📸 @a")
    x = FakeX()
    result = await run_daily(pool, FakeStorage(), x, today=DAY, enabled=True)
    assert result.outcome == "posted" and result.pick_id == older
    assert x.calls[1] == ("post", "Spotted in A 📸 @a", "m1")
    row = await _row(migrated_db, older)
    assert (row["status"], row["posted_on"], row["x_post_id"]) == ("posted", DAY, "p2")
    assert row["posted_at"] is not None and row["last_error"] is None
    assert (await _row(migrated_db, newer))["status"] == "queued"


async def test_second_run_same_day_posts_nothing(migrated_db, pool):
    await _queued(migrated_db)
    await _queued(migrated_db, picked_offset=60)
    x = FakeX()
    assert (await run_daily(pool, FakeStorage(), x, today=DAY, enabled=True)).outcome == "posted"
    again = await run_daily(pool, FakeStorage(), x, today=DAY, enabled=True)
    assert again.outcome == "already_posted_today"
    assert len(x.calls) == 2
    # The next day the second pick goes.
    assert (await run_daily(pool, FakeStorage(), x, today=DAY + timedelta(days=1),
                            enabled=True)).outcome == "posted"


async def test_empty_queue_is_quiet(migrated_db, pool):
    x = FakeX()
    assert (await run_daily(pool, FakeStorage(), x, today=DAY, enabled=True)).outcome == "empty"
    assert x.calls == []


@pytest.mark.parametrize("change", ["rejected", "duplicate", "unshared", "consent_removed"])
async def test_ineligible_head_is_skipped(migrated_db, pool, change):
    head, sid, oid = await _queued(migrated_db, picked_offset=0)
    nxt, _, _ = await _queued(migrated_db, picked_offset=60)
    if change == "rejected":
        await migrated_db.execute("UPDATE sightings SET review_status='rejected' WHERE id=$1", sid)
    elif change == "duplicate":
        other, _ = await fx.legacy_sighting(migrated_db, oid)
        await migrated_db.execute("UPDATE sightings SET duplicate_of=$2 WHERE id=$1", sid, other)
    elif change == "unshared":
        await migrated_db.execute("UPDATE sightings SET shared_with_team_at=NULL WHERE id=$1", sid)
    else:
        await migrated_db.execute("UPDATE observers SET social_consent=false WHERE id=$1", oid)

    result = await run_daily(pool, FakeStorage(), FakeX(), today=DAY, enabled=True)
    assert result.outcome == "posted" and result.pick_id == nxt
    skipped = await _row(migrated_db, head)
    assert (skipped["status"], skipped["last_error"]) == ("queued", "no longer eligible")


async def test_rejected_by_x_requeues_with_error(migrated_db, pool):
    pick_id, _, _ = await _queued(migrated_db)
    x = FakeX(create_error=XRejected("post refused: 403 no credits"))
    result = await run_daily(pool, FakeStorage(), x, today=DAY, enabled=True)
    assert result.outcome == "failed"
    row = await _row(migrated_db, pick_id)
    assert row["status"] == "queued" and "no credits" in row["last_error"]
    assert row["posted_on"] is None and row["last_attempt_at"] is not None
    # Tomorrow it is tried again.
    ok = await run_daily(pool, FakeStorage(), FakeX(), today=DAY + timedelta(days=1), enabled=True)
    assert ok.outcome == "posted" and ok.pick_id == pick_id


async def test_upload_failure_requeues(migrated_db, pool):
    pick_id, _, _ = await _queued(migrated_db)
    x = FakeX(upload_error=XRejected("media upload refused: 500"))
    assert (await run_daily(pool, FakeStorage(), x, today=DAY, enabled=True)).outcome == "failed"
    assert (await _row(migrated_db, pick_id))["status"] == "queued"
    assert [c[0] for c in x.calls] == ["upload"]


async def test_timeout_on_create_leaves_posting_and_never_reposts(migrated_db, pool):
    pick_id, _, _ = await _queued(migrated_db)
    await _queued(migrated_db, picked_offset=60)
    x = FakeX(create_error=XUncertain("no answer while creating the post"))
    assert (await run_daily(pool, FakeStorage(), x, today=DAY, enabled=True)).outcome == "failed"
    row = await _row(migrated_db, pick_id)
    assert row["status"] == "posting" and "no answer" in row["last_error"]

    later = FakeX()
    blocked = await run_daily(pool, FakeStorage(), later, today=DAY + timedelta(days=1), enabled=True)
    assert blocked.outcome == "blocked_by_uncertain"
    assert later.calls == []


async def test_a_refused_pick_does_not_block_the_queue(migrated_db, pool):
    """A pick X keeps refusing (a caption X counts as too long, duplicate text)
    would otherwise sit at the head and stop every post behind it."""
    stuck, _, _ = await _queued(migrated_db, picked_offset=0)
    behind, _, _ = await _queued(migrated_db, picked_offset=60)
    refused = FakeX(create_error=XRejected("post refused: 403 duplicate content"))
    assert (await run_daily(pool, FakeStorage(), refused, today=DAY, enabled=True)).outcome == "failed"
    nxt = await run_daily(pool, FakeStorage(), FakeX(), today=DAY + timedelta(days=1), enabled=True)
    assert nxt.outcome == "posted" and nxt.pick_id == behind
    row = await _row(migrated_db, stuck)
    assert row["status"] == "queued" and "duplicate content" in row["last_error"]
