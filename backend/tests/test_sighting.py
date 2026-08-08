import io
from uuid import UUID

import pytest
from PIL import Image


def _jpeg():
    b = io.BytesIO()
    Image.new("RGB", (400, 300), (100, 140, 90)).save(b, "JPEG")
    return b.getvalue()


@pytest.mark.asyncio
async def test_post_sighting_requires_auth(app_client):
    r = await app_client.post(
        "/sighting",
        files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
        data={"geo_source": "none", "captured_at": "2026-07-19T10:00:00Z"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_post_sighting_creates_rows(authed_client):
    client, oid = authed_client
    r = await client.post(
        "/sighting",
        files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
        data={
            "lat": "12.97",
            "lng": "77.59",
            "geo_source": "device_gps",
            "geo_accuracy_m": "8.0",
            "captured_at": "2026-07-19T10:00:00Z",
        },
    )
    assert r.status_code == 201
    sid = r.json()["sighting_id"]
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT observer_id, individual_id, match_status, review_status, "
            "ST_Y(geog::geometry) AS lat FROM sightings WHERE id=$1",
            sid,
        )
        assert row["observer_id"] == oid
        assert row["individual_id"] is None
        assert row["match_status"] == "unmatched" and row["review_status"] == "valid"
        assert abs(row["lat"] - 12.97) < 1e-6
        n = await c.fetchval("SELECT count(*) FROM photos WHERE sighting_id=$1", sid)
        assert n == 1


@pytest.mark.asyncio
async def test_post_sighting_stores_optional_attrs(authed_client):
    client, _ = authed_client
    r = await client.post(
        "/sighting",
        files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
        data={
            "geo_source": "none",
            "captured_at": "2026-07-19T10:00:00Z",
            "sex": "female",
            "ear_notch": "left",
            "condition": "injured",
            "note": "friendly",
        },
    )
    assert r.status_code == 201
    sid = r.json()["sighting_id"]
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        attrs = await c.fetchval("SELECT attrs FROM sightings WHERE id=$1", sid)
    import json as _json

    parsed = _json.loads(attrs) if isinstance(attrs, str) else attrs
    assert parsed["sex"] == "female"
    assert parsed["ear_notch"] == "left"
    assert parsed["condition"] == "injured"
    assert parsed["note"] == "friendly"


@pytest.mark.asyncio
async def test_post_sighting_geo_none_stores_null_geog(authed_client):
    client, _ = authed_client
    r = await client.post(
        "/sighting",
        files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
        data={"geo_source": "none", "captured_at": "2026-07-19T10:00:00Z"},
    )
    assert r.status_code == 201
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        g = await c.fetchval("SELECT geog FROM sightings WHERE id=$1", r.json()["sighting_id"])
    assert g is None


@pytest.mark.asyncio
async def test_post_sighting_saves_when_no_dog_detected(authed_client):
    """The detector is a label, never a gate. A flat green rectangle contains
    no dog and scores near zero -- it must still be saved, with the photo
    stored, because a capture is often the only copy of that photo anywhere."""
    client, _ = authed_client
    r = await client.post(
        "/sighting",
        files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
        data={"geo_source": "none", "captured_at": "2026-07-19T10:00:00Z"},
    )
    assert r.status_code == 201
    sid = r.json()["sighting_id"]
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT dog_confidence, review_status FROM sightings WHERE id=$1", sid
        )
        n_photos = await c.fetchval(
            "SELECT count(*) FROM photos WHERE sighting_id=$1", sid
        )
    from app.detect import DOG_CONF_THRESHOLD

    # Scored, saved, and visible -- the low score is recorded, not acted on.
    assert row["dog_confidence"] is not None
    assert row["dog_confidence"] < DOG_CONF_THRESHOLD
    assert row["review_status"] == "valid"
    assert n_photos == 1


@pytest.mark.asyncio
async def test_post_sighting_records_dog_confidence(authed_client):
    """The label is persisted so we can tune the threshold from real captures
    instead of guessing at synthetic ones."""
    client, _ = authed_client
    r = await client.post(
        "/sighting",
        files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
        data={"geo_source": "none", "captured_at": "2026-07-19T10:00:00Z"},
    )
    conf = None
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        conf = await c.fetchval(
            "SELECT dog_confidence FROM sightings WHERE id=$1", r.json()["sighting_id"]
        )
    assert conf is not None and 0.0 <= conf <= 1.0


@pytest.mark.asyncio
async def test_post_sighting_accepts_legacy_override_field(authed_client):
    """Queued offline captures from older clients still send override_no_dog.
    Replaying one must not 422 -- that would strand the sighting."""
    client, _ = authed_client
    r = await client.post(
        "/sighting",
        files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
        data={
            "geo_source": "none",
            "captured_at": "2026-07-19T10:00:00Z",
            "override_no_dog": "true",
        },
    )
    assert r.status_code == 201


@pytest.mark.asyncio
async def test_post_sighting_dog_confidence_null_until_background_task_runs(
    authed_client, monkeypatch
):
    """The insert itself must not depend on the detector: even if scoring is
    slow or fails, the row exists with dog_confidence NULL until the
    background task updates it."""
    from app.routes import sighting as sighting_route

    def boom(_raw):
        raise RuntimeError("simulated detector failure")

    # sighting.py does `from app.detect_reid import animal_confidence`, which
    # binds its own name in this module's namespace -- patch that name, not
    # app.detect_reid's, or the patch has no effect on the code under test.
    monkeypatch.setattr(sighting_route, "animal_confidence", boom)

    client, _ = authed_client
    r = await client.post(
        "/sighting",
        files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
        data={"geo_source": "none", "captured_at": "2026-07-19T10:00:00Z"},
    )
    assert r.status_code == 201
    sid = r.json()["sighting_id"]
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT dog_confidence, review_status FROM sightings WHERE id=$1", sid
        )
    # Detector failure fails open: sighting still saved, just unscored.
    assert row["dog_confidence"] is None
    assert row["review_status"] == "valid"


@pytest.mark.asyncio
async def test_post_sighting_insert_does_not_call_detector_directly(
    authed_client, monkeypatch
):
    """Regression guard for the whole point of this change: the detector
    must be invoked from the background task, not inline in the request
    handler, so a call inside `create_sighting`'s own body (before the
    response is built) never happens."""
    from app.routes import sighting as sighting_route

    calls = []
    orig = sighting_route._score_and_save_dog_confidence

    async def spy(pool, sighting_id, raws):
        calls.append(sighting_id)
        await orig(pool, sighting_id, raws)

    monkeypatch.setattr(sighting_route, "_score_and_save_dog_confidence", spy)

    client, _ = authed_client
    r = await client.post(
        "/sighting",
        files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
        data={"geo_source": "none", "captured_at": "2026-07-19T10:00:00Z"},
    )
    assert r.status_code == 201
    sid = r.json()["sighting_id"]
    assert calls == [UUID(sid)]


@pytest.mark.asyncio
async def test_get_match_does_not_mutate_proposals(authed_client):
    """GET /sighting/{id}/match must be a pure read.

    It used to call resolve_sighting, which deletes and recreates pending
    proposals -- so polling handed out a proposal ID and then invalidated it,
    and acting on the ID you were just given returned 404. Resolution now runs
    once in the background task that writes the embeddings.
    """
    from app.ids import uuid7

    client, obs = authed_client
    pool = client._transport.app.state.pool
    sid, pid, eid = uuid7(), uuid7(), uuid7()
    prop = uuid7()
    async with pool.acquire() as c:
        await c.execute(
            "INSERT INTO sightings (id, observer_id, captured_at, match_status) "
            "VALUES ($1,$2,now(),'proposed')", sid, obs)
        await c.execute(
            "INSERT INTO photos (id, sighting_id, s3_key) VALUES ($1,$2,'k.webp')", pid, sid)
        await c.execute(
            "INSERT INTO embeddings (id, photo_id, model, dim, vec_miew) "
            "VALUES ($1,$2,'miewid-msv3',2152,$3::vector)",
            eid, pid, "[" + ",".join(["0.01"] * 2152) + "]")
        await c.execute(
            "INSERT INTO match_proposals (id, sighting_id, candidate_sighting_id, "
            "score, method, status) VALUES ($1,$2,$3,0.9,'miewid-msv3','pending')",
            prop, sid, sid)

    for _ in range(3):
        r = await client.get(f"/sighting/{sid}/match")
        assert r.status_code == 200, r.text

    async with pool.acquire() as c:
        still = await c.fetchval(
            "SELECT id FROM match_proposals WHERE sighting_id=$1 AND status='pending'", sid)
    assert still == prop, "polling must not replace the proposal it handed out"
