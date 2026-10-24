"""The same photograph in two sightings is the same dog, linked without asking.

32 of the 33 pending proposals in production on 2026-10-23 were one photo
attached to two sightings. These tests pin the rule that keeps those out of the
review queue -- and, as much as that, the cases where it must step aside and
leave the question to a person: a photo of two dogs, two sightings that are
already different dogs, two animals a capture separated.

No model is involved. Vectors are synthetic, and the hash is whatever string
the fixture says, because what is under test is the linking, not the hashing.
The hashing has its own tests at the bottom.
"""

import hashlib
import io

import asyncpg
import numpy as np
import pytest
from PIL import Image

from app.config import settings
from app.embed import EMBED_DIM, MODEL_NAME
from app.identical import link_identical
from app.ids import uuid7
from app.matching import resolve_sighting

SHA = "a" * 64
OTHER_SHA = "b" * 64


def _unit(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(EMBED_DIM).astype(np.float32)
    return v / np.linalg.norm(v)


def _lit(v) -> str:
    return "[" + ",".join(f"{float(x):.7g}" for x in v) + "]"


async def _observer(conn):
    oid = uuid7()
    await conn.execute(
        "INSERT INTO observers (id, display_name, created_via) VALUES ($1,'T','t')", oid)
    return oid


async def _individual(conn):
    iid = uuid7()
    await conn.execute(
        "INSERT INTO individuals (id, created_by, created_via, status) "
        "VALUES ($1,'feeder','match_confirmation','active')", iid)
    return iid


async def _sighting(conn, oid, *, sha=SHA, seed=1, individual_id=None,
                    status="unmatched", species=None, review="valid"):
    """A /sighting upload: one photo carrying the hash, embedded."""
    sid, pid = uuid7(), uuid7()
    await conn.execute(
        "INSERT INTO sightings (id, observer_id, captured_at, geo_source, individual_id, "
        "match_status, species, review_status) VALUES ($1,$2,now(),'none',$3,$4,$5,$6)",
        sid, oid, individual_id, status, species, review)
    await conn.execute(
        "INSERT INTO photos (id, sighting_id, s3_key, content_sha256) VALUES ($1,$2,$3,$4)",
        pid, sid, f"sightings/{sid}/{pid}.webp", sha)
    await conn.execute(
        f"INSERT INTO embeddings (id, photo_id, model, dim, vec_miew) "
        f"VALUES ($1,$2,$3,$4,$5::vector({EMBED_DIM}))",
        uuid7(), pid, MODEL_NAME, EMBED_DIM, _lit(_unit(seed)))
    return sid


async def _capture(conn, oid, *, sha=SHA, seeds=(1,)):
    """A /capture upload: one hashed source photo and one published sighting
    per animal found in it, each holding only its crop."""
    cid, src = uuid7(), uuid7()
    await conn.execute(
        "INSERT INTO captures (id, observer_id, kind, captured_at, processing_state) "
        "VALUES ($1,$2,'photo',now(),'ready')", cid, oid)
    await conn.execute(
        "INSERT INTO photos (id, capture_id, s3_key, content_sha256) VALUES ($1,$2,$3,$4)",
        src, cid, f"captures/{cid}/sources/{src}.webp", sha)
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
            iid, cid, src, crop, sid, f"track-{n}")
        await conn.execute(
            f"INSERT INTO embeddings (id, photo_id, instance_id, model, dim, vec_miew) "
            f"VALUES ($1,$2,$3,$4,$5,$6::vector({EMBED_DIM}))",
            uuid7(), crop, iid, MODEL_NAME, EMBED_DIM, _lit(_unit(seed)))
        sids.append(sid)
    return sids


async def _resolve(conn, sid):
    return await resolve_sighting(conn, sid, auto_merge_min=1.01, propose_min=0.5,
                                  radius_m=1000, max_candidates=5, new_uuid=uuid7)


async def _state(conn, sid):
    return await conn.fetchrow(
        "SELECT individual_id, match_status FROM sightings WHERE id=$1", sid)


async def _pending(conn):
    return await conn.fetchval("SELECT count(*) FROM match_proposals WHERE status='pending'")


async def _link(conn, sid):
    async with conn.transaction():
        return await link_identical(conn, sid, new_uuid=uuid7)


# ---------------------------------------------------------------------------
# It links
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_one_photo_picked_twice_is_one_dog_without_a_proposal(migrated_db):
    """The production case. Without the rule this pair is a proposal at ~1.0
    similarity, which is precisely what filled the queue."""
    conn = migrated_db
    oid = await _observer(conn)
    first = await _sighting(conn, oid)
    await _resolve(conn, first)
    again = await _sighting(conn, oid)

    outcome = await _resolve(conn, again)

    assert outcome.status == "confirmed"
    a, b = await _state(conn, first), await _state(conn, again)
    assert a["individual_id"] is not None
    assert a["individual_id"] == b["individual_id"] == outcome.individual_id
    assert a["match_status"] == b["match_status"] == "confirmed"
    assert await conn.fetchval("SELECT count(*) FROM match_proposals") == 0

    dog = await conn.fetchrow("SELECT * FROM individuals WHERE id=$1", outcome.individual_id)
    assert (dog["created_by"], dog["created_via"]) == ("model", "identical_photo")
    assert dog["created_by_observer"] is None, "nobody decided this"
    assert dog["first_seen_at"] is not None and dog["last_seen_at"] is not None


@pytest.mark.asyncio
async def test_it_is_not_recorded_as_a_human_verdict(migrated_db):
    """`confirmations` is the calibration set; a rule that always says "same"
    would poison it. The audit trail lives in `auto_links` instead."""
    conn = migrated_db
    oid = await _observer(conn)
    first = await _sighting(conn, oid)
    again = await _sighting(conn, oid)
    await _resolve(conn, again)

    assert await conn.fetchval("SELECT count(*) FROM confirmations") == 0
    links = await conn.fetch(
        "SELECT sighting_id, matched_sighting_id, method, content_sha256 FROM auto_links")
    assert {(r["sighting_id"], r["matched_sighting_id"]) for r in links} == {
        (again, first), (first, again)}
    assert {r["method"] for r in links} == {"identical_photo"}
    assert {r["content_sha256"] for r in links} == {SHA}


@pytest.mark.asyncio
async def test_across_observers_too(migrated_db):
    """A forwarded photo uploaded by a second person is still the same dog, and
    unlike a proposal this needs no one with standing over both sightings."""
    conn = migrated_db
    mine = await _sighting(conn, await _observer(conn))
    theirs = await _sighting(conn, await _observer(conn), seed=2)

    outcome = await _resolve(conn, theirs)

    assert outcome.status == "confirmed"
    assert (await _state(conn, mine))["individual_id"] == outcome.individual_id


@pytest.mark.asyncio
async def test_links_through_a_capture_source_photo(migrated_db):
    """A /capture sighting holds only a crop. It is matched through the source
    photo its animal was cut from, so the two upload paths agree."""
    conn = migrated_db
    oid = await _observer(conn)
    legacy = await _sighting(conn, oid)
    [captured] = await _capture(conn, oid)

    outcome = await _resolve(conn, captured)

    assert outcome.status == "confirmed"
    assert (await _state(conn, legacy))["individual_id"] == outcome.individual_id


@pytest.mark.asyncio
async def test_reuses_the_identity_one_side_already_has(migrated_db):
    """A human-confirmed sighting keeps its dog; the copy joins it. Nothing is
    un-linked, re-linked or minted."""
    conn = migrated_db
    oid = await _observer(conn)
    dog = await _individual(conn)
    confirmed = await _sighting(conn, oid, individual_id=dog, status="confirmed")
    await conn.execute(
        "INSERT INTO confirmations (id, sighting_id, individual_id, observer_id, verdict) "
        "VALUES ($1,$2,$3,$4,'same')", uuid7(), confirmed, dog, oid)
    copy = await _sighting(conn, oid)

    outcome = await _resolve(conn, copy)

    assert outcome.individual_id == dog
    assert (await _state(conn, copy))["individual_id"] == dog
    assert dict(await _state(conn, confirmed)) == {"individual_id": dog, "match_status": "confirmed"}
    assert await conn.fetchval("SELECT count(*) FROM individuals") == 1
    assert await conn.fetchval("SELECT count(*) FROM confirmations") == 1
    assert await conn.fetchval("SELECT count(*) FROM auto_links WHERE sighting_id=$1", confirmed) == 0
    span = await conn.fetchrow("SELECT first_seen_at, last_seen_at FROM individuals WHERE id=$1", dog)
    assert span["first_seen_at"] is not None and span["last_seen_at"] is not None

    # And the human's row survives every later pass, of either kind.
    await _resolve(conn, confirmed)
    await _link(conn, confirmed)
    assert dict(await _state(conn, confirmed)) == {"individual_id": dog, "match_status": "confirmed"}


@pytest.mark.asyncio
async def test_clears_the_proposal_it_answered_and_nothing_else(migrated_db):
    """The pair's pending proposal is a settled question and goes. A proposal
    to a sighting outside the new identity is still a real question."""
    conn = migrated_db
    oid = await _observer(conn)
    a = await _sighting(conn, oid)
    b = await _sighting(conn, oid)
    elsewhere = await _sighting(conn, oid, sha=OTHER_SHA, seed=9)
    for target in (a, elsewhere):
        await conn.execute(
            "INSERT INTO match_proposals (id, sighting_id, candidate_sighting_id, score, "
            "method, status) VALUES ($1,$2,$3,0.99,$4,'pending')", uuid7(), b, target, MODEL_NAME)

    out = await _link(conn, a)

    assert out.status == "linked" and out.proposals_cleared == 1
    left = await conn.fetch("SELECT candidate_sighting_id FROM match_proposals WHERE status='pending'")
    assert [r["candidate_sighting_id"] for r in left] == [elsewhere]


# ---------------------------------------------------------------------------
# A photo logged twice counts once
# ---------------------------------------------------------------------------

async def _dup(conn, sid):
    return await conn.fetchval("SELECT duplicate_of FROM sightings WHERE id=$1", sid)


@pytest.mark.asyncio
async def test_the_later_copy_is_marked_and_the_original_is_not(migrated_db):
    conn = migrated_db
    oid = await _observer(conn)
    first = await _sighting(conn, oid)
    again = await _sighting(conn, oid)

    await _resolve(conn, again)

    assert await _dup(conn, again) == first
    assert await _dup(conn, first) is None
    assert await conn.fetchval("SELECT count(*) FROM sightings") == 2, "nothing is deleted"


@pytest.mark.asyncio
async def test_a_sighting_with_a_photo_of_its_own_still_counts(migrated_db):
    """Linked, because they share a photo; not hidden, because the second
    sighting holds a photo the first does not -- it is a second observation."""
    conn = migrated_db
    oid = await _observer(conn)
    first = await _sighting(conn, oid)
    more = await _sighting(conn, oid)
    await conn.execute(
        "INSERT INTO photos (id, sighting_id, s3_key, content_sha256) VALUES ($1,$2,'x.webp',$3)",
        uuid7(), more, OTHER_SHA)

    outcome = await _resolve(conn, more)

    assert outcome.status == "confirmed"
    assert await _dup(conn, more) is None and await _dup(conn, first) is None


@pytest.mark.asyncio
async def test_a_third_copy_points_at_the_original_not_at_a_copy(migrated_db):
    conn = migrated_db
    oid = await _observer(conn)
    first = await _sighting(conn, oid)
    second = await _sighting(conn, oid)
    await _resolve(conn, second)
    third = await _sighting(conn, oid)

    await _resolve(conn, third)

    assert await _dup(conn, second) == first
    assert await _dup(conn, third) == first


@pytest.mark.asyncio
async def test_copies_are_never_offered_as_candidates(migrated_db):
    """A copy is its original's photo again; proposing both asks one question
    twice."""
    conn = migrated_db
    oid = await _observer(conn)
    first = await _sighting(conn, oid)
    again = await _sighting(conn, oid)
    await _resolve(conn, again)
    lookalike = await _sighting(conn, oid, sha=OTHER_SHA)

    outcome = await _resolve(conn, lookalike)

    assert outcome.status == "proposed"
    assert [c.sighting_id for c in outcome.candidates] == [first]


@pytest.mark.asyncio
async def test_the_dog_is_seen_once_on_every_public_surface(authed_client):
    """/dogs, /map and /stats all read `countable_sighting()`; a dog must not be
    "seen twice" because one photo was submitted twice."""
    client, oid = authed_client
    pool = client._transport.app.state.pool
    async with pool.acquire() as conn:
        first = await _sighting(conn, oid)
        again = await _sighting(conn, oid)
        await conn.execute(
            "UPDATE sightings SET geog=ST_SetSRID(ST_MakePoint(77.59,12.97),4326)::geography, "
            "geo_source='device_gps'")
        await _resolve(conn, again)
        assert await _dup(conn, again) == first

    dogs = (await client.get("/dogs")).json()["dogs"]
    assert len(dogs) == 1
    assert dogs[0]["sighting_count"] == 1
    assert len(dogs[0]["photos"]) == 1

    pins = (await client.get("/map")).json()["sightings"]
    assert [p["id"] for p in pins] == [str(first)]

    totals = (await client.get("/stats")).json()["totals"]
    assert (totals["sightings"], totals["confirmed_individuals"]) == (1, 1)

    # Still theirs: the contributor did submit it twice, and /dex says so.
    dex = (await client.get("/dex")).json()
    assert len(dex["sightings"]) == 2


# ---------------------------------------------------------------------------
# Known names carry over
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_typed_name_is_proposed_for_the_dog(migrated_db):
    """Exactly as after a human `same`: proposed, never made the dog's name."""
    conn = migrated_db
    oid = await _observer(conn)
    first = await _sighting(conn, oid)
    [named] = await _capture(conn, oid)
    await conn.execute(
        """UPDATE sightings SET attrs='{"known_name":"Kaju"}' WHERE id=$1""", named)

    outcome = await _resolve(conn, named)
    await _link(conn, first)  # a second pass proposes nothing new

    rows = await conn.fetch("SELECT individual_id, name, status, proposed_by FROM individual_names")
    assert [(r["individual_id"], r["name"], r["status"], r["proposed_by"]) for r in rows] == [
        (outcome.individual_id, "Kaju", "proposed", oid)]
    assert await conn.fetchval(
        "SELECT name FROM individuals WHERE id=$1", outcome.individual_id) is None


# ---------------------------------------------------------------------------
# It steps aside
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_photo_of_two_dogs_goes_to_a_person(migrated_db):
    """Two dogs in one frame: the bytes say nothing about which dog either
    sighting is. The /sighting copy is ambiguous too, even though that pipeline
    only ever looked at one box."""
    conn = migrated_db
    oid = await _observer(conn)
    left, right = await _capture(conn, oid, seeds=(1, 2))
    legacy = await _sighting(conn, oid, seed=1)

    outcome = await _resolve(conn, legacy)

    assert outcome.status == "proposed", "falls back to the ordinary proposal"
    assert (await _state(conn, legacy))["individual_id"] is None
    assert (await _state(conn, left))["individual_id"] is None
    assert await conn.fetchval("SELECT count(*) FROM individuals") == 0
    assert (await _link(conn, legacy)).reason == "multi_animal"


@pytest.mark.asyncio
async def test_two_sightings_that_are_already_different_dogs_stay_apart(migrated_db):
    """Folding two identities together is a merge of individuals, which a rule
    is not entitled to do -- either may be a human verdict."""
    conn = migrated_db
    oid = await _observer(conn)
    one, two = await _individual(conn), await _individual(conn)
    a = await _sighting(conn, oid, individual_id=one, status="confirmed")
    b = await _sighting(conn, oid, individual_id=two, status="confirmed")

    out = await _link(conn, a)
    assert (out.status, out.reason) == ("skipped", "conflicting_identities")

    # A third copy cannot pick a side either, so it becomes a proposal.
    c = await _sighting(conn, oid)
    outcome = await _resolve(conn, c)
    assert outcome.status == "proposed"
    assert (await _state(conn, c))["individual_id"] is None
    assert (await _state(conn, a))["individual_id"] == one
    assert (await _state(conn, b))["individual_id"] == two


@pytest.mark.asyncio
async def test_incompatible_observations_are_not_linked(migrated_db):
    """`observations_compatible` is the same guard the human path uses."""
    conn = migrated_db
    oid = await _observer(conn)
    await _sighting(conn, oid, species="dog")
    cat = await _sighting(conn, oid, species="cat")

    out = await _link(conn, cat)

    assert (out.status, out.reason) == ("skipped", "incompatible")
    assert await conn.fetchval("SELECT count(*) FROM individuals") == 0


@pytest.mark.asyncio
async def test_a_rejected_copy_seeds_nothing(migrated_db):
    conn = migrated_db
    oid = await _observer(conn)
    await _sighting(conn, oid, review="rejected")
    fresh = await _sighting(conn, oid)

    assert await _link(conn, fresh) is None
    assert await conn.fetchval("SELECT count(*) FROM individuals") == 0


@pytest.mark.asyncio
async def test_a_different_photo_changes_nothing(migrated_db):
    """No shared hash, no opinion: resolution proceeds exactly as before."""
    conn = migrated_db
    oid = await _observer(conn)
    await _sighting(conn, oid, sha=OTHER_SHA)
    me = await _sighting(conn, oid)

    outcome = await _resolve(conn, me)
    assert outcome.status == "proposed"
    assert await conn.fetchval("SELECT count(*) FROM individuals") == 0


# ---------------------------------------------------------------------------
# The hash itself
# ---------------------------------------------------------------------------

def _jpeg(colour=(100, 140, 90)):
    b = io.BytesIO()
    Image.new("RGB", (400, 300), colour).save(b, "JPEG")
    return b.getvalue()


def test_the_stored_encode_is_deterministic():
    """Everything rests on this: the hash is of the stored WebP, so one upload
    must encode to the same bytes every time or nothing would ever match."""
    from app.photos import process_photo

    raw = _jpeg()
    first, second = process_photo(raw), process_photo(raw)
    assert first.original == second.original
    assert first.content_sha256 == hashlib.sha256(first.original).hexdigest()
    assert process_photo(_jpeg((1, 2, 3))).content_sha256 != first.content_sha256


@pytest.mark.asyncio
async def test_a_sighting_upload_stores_the_hash_of_what_it_stored(authed_client):
    client, _ = authed_client
    r = await client.post(
        "/sighting",
        files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
        data={"geo_source": "none", "captured_at": "2026-10-23T10:00:00Z"},
    )
    assert r.status_code == 201
    from app.deps import get_storage
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT s3_key, content_sha256 FROM photos WHERE sighting_id=$1",
            r.json()["sighting_id"])
    stored = await get_storage().get(row["s3_key"])
    assert row["content_sha256"] == hashlib.sha256(stored).hexdigest()


@pytest.mark.asyncio
async def test_a_capture_upload_hashes_its_sources(authed_client, monkeypatch):
    client, _ = authed_client
    monkeypatch.setattr(settings, "multi_animal_enabled", True)
    monkeypatch.setattr(settings, "media_jobs_enabled", True)
    r = await client.post(
        "/capture",
        files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
        data={"geo_source": "none", "captured_at": "2026-10-23T10:00:00Z"},
    )
    assert r.status_code == 201, r.text
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        sha = await c.fetchval(
            "SELECT content_sha256 FROM photos WHERE capture_id=$1", r.json()["capture_id"])
    assert sha is not None and len(sha) == 64


# ---------------------------------------------------------------------------
# The backfill
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_backfill_dry_run_writes_nothing_and_apply_matches_it(migrated_db):
    """The dry run does the whole job and rolls it back, so its numbers are
    the real run's numbers -- and nothing it did survives."""
    from app.storage.s3 import storage_from_settings
    from scripts.merge_identical_photos import run

    conn = migrated_db
    storage = storage_from_settings()
    await storage.ensure_bucket()
    oid = await _observer(conn)
    a = await _sighting(conn, oid, sha=None)
    b = await _sighting(conn, oid, sha=None, seed=2)
    clip = await _sighting(conn, oid, sha=None, seed=3)
    await conn.execute("UPDATE sightings SET attrs='{\"source\":\"video\"}' WHERE id=$1", clip)
    raw = _jpeg()
    for sid in (a, b, clip):
        key = await conn.fetchval("SELECT s3_key FROM photos WHERE sighting_id=$1", sid)
        await storage.put(key, raw, "image/webp")
    await conn.execute(
        "INSERT INTO match_proposals (id, sighting_id, candidate_sighting_id, score, method, "
        "status) VALUES ($1,$2,$3,0.99,$4,'pending')", uuid7(), b, a, MODEL_NAME)
    await conn.execute("UPDATE sightings SET match_status='proposed' WHERE id=$1", b)

    pool = await asyncpg.create_pool(settings.test_database_url, min_size=1, max_size=2)
    try:
        dry = await run(pool, storage, apply=False)

        # Clip frames are not photos anyone picked, and are never hashed.
        assert (dry.to_hash, dry.hashed) == (2, 2)
        assert dry.sightings_linked == 2 and dry.individuals_minted == 1
        assert dry.proposals_cleared == 1 and dry.duplicates_marked == 1
        assert await conn.fetchval("SELECT count(*) FROM sightings WHERE duplicate_of IS NOT NULL") == 0
        assert await conn.fetchval("SELECT count(*) FROM photos WHERE content_sha256 IS NOT NULL") == 0
        assert await conn.fetchval("SELECT count(*) FROM individuals") == 0
        assert await conn.fetchval("SELECT count(*) FROM auto_links") == 0
        assert await _pending(conn) == 1
        assert dict(await _state(conn, b)) == {"individual_id": None, "match_status": "proposed"}

        real = await run(pool, storage, apply=True)
        assert (real.sightings_linked, real.individuals_minted, real.proposals_cleared) == (2, 1, 1)
        assert (await _state(conn, a))["individual_id"] == (await _state(conn, b))["individual_id"] is not None
        assert (await _state(conn, clip))["individual_id"] is None
        assert await conn.fetchval("SELECT duplicate_of FROM sightings WHERE id=$1", b) == a
        assert await conn.fetchval("SELECT duplicate_of FROM sightings WHERE id=$1", a) is None
        assert await _pending(conn) == 0

        again = await run(pool, storage, apply=True)
        assert (again.to_hash, again.sightings_linked) == (0, 0), "a second run has nothing to do"
    finally:
        await pool.close()
