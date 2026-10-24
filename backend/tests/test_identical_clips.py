"""The same clip uploaded twice is the same dog, linked without asking.

On 2026-10-24 sixteen of seventeen pending proposals were one nine-frame clip
uploaded six times. The identical-photo rule never saw them: clip frames carry
no content hash, because a frame is sampled rather than chosen. The clip itself
is chosen, so it gets the hash (migration 0018) and the rule treats it like a
photo picked twice -- including stepping aside when the clip holds more than
one animal and a copy cannot say which is which.
"""

import hashlib
import io
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest

from app.config import settings
from app.embed import EMBED_DIM, MODEL_NAME
from app.ids import uuid7
from tests.test_identical_merge import _link, _lit, _observer, _pending, _resolve, _unit

CLIP = "c" * 64
OTHER_CLIP = "d" * 64


async def _clip_sighting(conn, oid, *, clip=CLIP, seed=1, frames=3):
    """A /sighting clip upload: unhashed frames, the clip's hash on the capture
    the 0015 trigger gives every sighting."""
    sid = uuid7()
    await conn.execute(
        "INSERT INTO sightings (id, observer_id, captured_at, geo_source, attrs, clip_s3_key) "
        "VALUES ($1,$2,now(),'none','{\"source\":\"video\"}',$3)",
        sid, oid, f"sightings/{sid}/clip.mp4")
    for _ in range(frames):
        pid = uuid7()
        await conn.execute(
            "INSERT INTO photos (id, sighting_id, s3_key) VALUES ($1,$2,$3)",
            pid, sid, f"sightings/{sid}/{pid}.webp")
        await conn.execute(
            f"INSERT INTO embeddings (id, photo_id, model, dim, vec_miew) "
            f"VALUES ($1,$2,$3,$4,$5::vector({EMBED_DIM}))",
            uuid7(), pid, MODEL_NAME, EMBED_DIM, _lit(_unit(seed)))
    await conn.execute(
        "UPDATE captures SET clip_sha256=$1 WHERE id=(SELECT capture_id FROM sightings WHERE id=$2)",
        clip, sid)
    return sid


async def _clip_capture(conn, oid, *, clip=CLIP, seeds=(1,)):
    """A /capture clip upload: one sampled frame, one published sighting per
    animal in it, each holding only its crop."""
    cid, frame = uuid7(), uuid7()
    await conn.execute(
        "INSERT INTO captures (id, observer_id, kind, captured_at, processing_state, "
        "clip_s3_key, clip_sha256) VALUES ($1,$2,'video',now(),'ready',$3,$4)",
        cid, oid, f"captures/{cid}/clip.mp4", clip)
    await conn.execute(
        "INSERT INTO photos (id, capture_id, s3_key) VALUES ($1,$2,$3)",
        frame, cid, f"captures/{cid}/evidence/{frame}.webp")
    sids = []
    for n, seed in enumerate(seeds):
        sid, crop, iid = uuid7(), uuid7(), uuid7()
        await conn.execute(
            "INSERT INTO sightings (id, capture_id, observer_id, captured_at, geo_source, "
            "species, vec_miew, processing_state) "
            f"VALUES ($1,$2,$3,now(),'none','dog',$4::vector({EMBED_DIM}),'ready')",
            sid, cid, oid, _lit(_unit(seed)))
        await conn.execute(
            "INSERT INTO photos (id, capture_id, sighting_id, s3_key) VALUES ($1,$2,$3,$4)",
            crop, cid, sid, f"captures/{cid}/evidence/{crop}.webp")
        await conn.execute(
            """INSERT INTO animal_instances (id, capture_id, source_photo_id, evidence_photo_id,
               sighting_id, track_id, species, confidence, bbox, crop_bbox)
               VALUES ($1,$2,$3,$4,$5,$6,'dog',0.9,'[0,0,1,1]','[0,0,1,1]')""",
            iid, cid, frame, crop, sid, f"track-{n}")
        await conn.execute(
            f"INSERT INTO embeddings (id, photo_id, instance_id, model, dim, vec_miew) "
            f"VALUES ($1,$2,$3,$4,$5,$6::vector({EMBED_DIM}))",
            uuid7(), crop, iid, MODEL_NAME, EMBED_DIM, _lit(_unit(seed)))
        sids.append(sid)
    return sids


@pytest.mark.asyncio
async def test_one_clip_uploaded_twice_is_one_dog_without_a_proposal(migrated_db):
    """The production case: every copy of the clip against every other."""
    conn = migrated_db
    oid = await _observer(conn)
    first = await _clip_sighting(conn, oid)
    await _resolve(conn, first)
    again = await _clip_sighting(conn, oid)
    await _resolve(conn, again)

    a, b = [await conn.fetchrow("SELECT individual_id, duplicate_of FROM sightings WHERE id=$1", s)
            for s in (first, again)]
    assert a["individual_id"] is not None and a["individual_id"] == b["individual_id"]
    assert await _pending(conn) == 0
    # The copy counts once, exactly as a photo picked twice does.
    assert a["duplicate_of"] is None and b["duplicate_of"] == first
    methods = {r["method"] for r in await conn.fetch("SELECT method FROM auto_links")}
    assert methods == {"identical_clip"}


@pytest.mark.asyncio
async def test_six_copies_of_one_clip_leave_nothing_to_ask(migrated_db):
    conn = migrated_db
    oid = await _observer(conn)
    sids = []
    for _ in range(6):
        sids.append(await _clip_sighting(conn, oid))
        await _resolve(conn, sids[-1])
    assert await _pending(conn) == 0
    assert await conn.fetchval(
        "SELECT count(DISTINCT individual_id) FROM sightings WHERE id = ANY($1::uuid[])", sids) == 1
    assert await conn.fetchval(
        "SELECT count(*) FROM sightings WHERE id = ANY($1::uuid[]) AND duplicate_of IS NULL", sids) == 1


@pytest.mark.asyncio
async def test_different_clips_are_left_to_matching(migrated_db):
    conn = migrated_db
    oid = await _observer(conn)
    await _clip_sighting(conn, oid, clip=CLIP)
    other = await _clip_sighting(conn, oid, clip=OTHER_CLIP)
    assert await _link(conn, other) is None


@pytest.mark.asyncio
async def test_a_clip_of_two_dogs_uploaded_twice_goes_to_a_person(migrated_db):
    """Two copies of a two-dog clip say both are the same pair, not which dog
    in one is which dog in the other."""
    conn = migrated_db
    oid = await _observer(conn)
    await _clip_capture(conn, oid, seeds=(1, 2))
    again = await _clip_capture(conn, oid, seeds=(1, 2))
    out = await _link(conn, again[0])
    assert out.status == "skipped" and out.reason == "multi_animal"


@pytest.mark.asyncio
async def test_a_one_dog_capture_clip_uploaded_twice_links(migrated_db):
    conn = migrated_db
    oid = await _observer(conn)
    [first] = await _clip_capture(conn, oid)
    [again] = await _clip_capture(conn, oid)
    out = await _link(conn, again)
    assert out.status == "linked" and out.partners == [first]
    assert await conn.fetchval("SELECT duplicate_of FROM sightings WHERE id=$1", again) == first


@pytest.mark.asyncio
async def test_a_clip_and_a_photo_do_not_collide(migrated_db):
    """A clip's hash and a photo's are different spaces; a photo whose hash
    happened to equal a clip's must not link to it."""
    from tests.test_identical_merge import _sighting
    conn = migrated_db
    oid = await _observer(conn)
    await _sighting(conn, oid, sha=CLIP)
    clip = await _clip_sighting(conn, oid, clip=CLIP)
    assert await _link(conn, clip) is None


@pytest.mark.asyncio
async def test_a_sighting_clip_upload_records_the_clip_hash(authed_client):
    from tests.test_video import _make_video, _varied_frames
    client, _ = authed_client
    video = _make_video(_varied_frames(6))
    r = await client.post(
        "/sighting",
        files={"video": ("clip.mp4", video, "video/mp4")},
        data={"geo_source": "none", "captured_at": "2026-07-19T10:00:00Z"},
    )
    assert r.status_code == 201, r.text
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        got = await c.fetchval(
            "SELECT c.clip_sha256 FROM sightings s JOIN captures c ON c.id = s.capture_id "
            "WHERE s.id = $1", r.json()["sighting_id"])
    assert got == hashlib.sha256(video).hexdigest()


@pytest.mark.asyncio
async def test_a_capture_clip_upload_records_the_clip_hash(migrated_db, monkeypatch):
    from fastapi import UploadFile
    from app.routes.capture import create_capture
    from tests.test_media_jobs import FakePool
    conn = migrated_db
    oid = uuid4()
    await conn.execute("INSERT INTO observers(id) VALUES($1)", oid)
    monkeypatch.setattr(settings, "multi_animal_enabled", True)
    monkeypatch.setattr(settings, "media_jobs_enabled", True)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=FakePool(conn))))
    raw = b"clip bytes"
    received = await create_capture(
        request=request, photos=None, video=UploadFile(io.BytesIO(raw)), lat=None, lng=None,
        geo_accuracy_m=None, client_token="t", geo_source="none",
        captured_at=datetime.now(timezone.utc), reported_at=None, note=None,
        observer_id=oid, storage=SimpleNamespace(put=AsyncMock()))
    got = await conn.fetchval("SELECT clip_sha256 FROM captures WHERE id=$1", received["capture_id"])
    assert got == hashlib.sha256(raw).hexdigest()


@pytest.mark.asyncio
async def test_backfill_hashes_stored_clips_and_links_their_copies(migrated_db):
    from app.storage.s3 import storage_from_settings
    from scripts.merge_identical_photos import run

    conn = migrated_db
    storage = storage_from_settings()
    await storage.ensure_bucket()
    oid = await _observer(conn)
    sids = [await _clip_sighting(conn, oid, clip=None) for _ in range(3)]
    for sid in sids:
        # Pre-0018 rows: the legacy capture carries the clip key, no hash.
        key = f"sightings/{sid}/clip.mp4"
        await conn.execute(
            "UPDATE captures SET clip_s3_key=$1 WHERE id=(SELECT capture_id FROM sightings WHERE id=$2)",
            key, sid)
        await storage.put(key, b"the same clip", "video/mp4")
        a, b = sids[0], sid
        if a != b:
            await conn.execute(
                "INSERT INTO match_proposals (id, sighting_id, candidate_sighting_id, score, method, "
                "status) VALUES ($1,$2,$3,0.64,$4,'pending')", uuid7(), b, a, MODEL_NAME)

    pool = await asyncpg.create_pool(settings.test_database_url, min_size=1, max_size=2)
    try:
        rep = await run(pool, storage, apply=True)
    finally:
        await pool.close()
    assert rep.clips_hashed == 3
    assert await _pending(conn) == 0
    assert await conn.fetchval(
        "SELECT count(DISTINCT clip_sha256) FROM captures c JOIN sightings s ON s.capture_id=c.id "
        "WHERE s.id = ANY($1::uuid[])", sids) == 1
    assert await conn.fetchval(
        "SELECT count(*) FROM sightings WHERE id = ANY($1::uuid[]) AND duplicate_of IS NULL", sids) == 1


@pytest.mark.asyncio
async def test_a_held_copy_showing_two_dogs_stops_the_other_copies_linking(migrated_db):
    """The /sighting pipeline only looks at the biggest animal. If a /capture
    copy of the same clip found two dogs in one frame -- even one still held,
    with no sightings yet -- no copy may say which dog it is."""
    conn = migrated_db
    oid = await _observer(conn)
    cid, frame = uuid7(), uuid7()
    await conn.execute(
        "INSERT INTO captures (id, observer_id, kind, captured_at, processing_state, clip_sha256) "
        "VALUES ($1,$2,'video',now(),'needs_review',$3)", cid, oid, CLIP)
    await conn.execute(
        "INSERT INTO photos (id, capture_id, s3_key) VALUES ($1,$2,$3)", frame, cid, "f.webp")
    for n in range(2):
        crop = uuid7()
        await conn.execute(
            "INSERT INTO photos (id, capture_id, s3_key) VALUES ($1,$2,$3)", crop, cid, f"c{n}.webp")
        await conn.execute(
            """INSERT INTO animal_instances (id, capture_id, source_photo_id, evidence_photo_id,
               track_id, species, confidence, bbox, crop_bbox)
               VALUES ($1,$2,$3,$4,$5,'dog',0.9,'[0,0,1,1]','[0,0,1,1]')""",
            uuid7(), cid, frame, crop, f"t{n}")
    await _clip_sighting(conn, oid)
    again = await _clip_sighting(conn, oid)
    out = await _link(conn, again)
    assert out.status == "skipped" and out.reason == "multi_animal"
