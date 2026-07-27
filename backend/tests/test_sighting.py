import io

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
