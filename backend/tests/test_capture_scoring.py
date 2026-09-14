"""What a capture records about what was in the frame.

The contract 0002 established and this must not break: scoring is a label,
never a gate. A detector failure costs the label and nothing else -- the
sighting exists, is visible, and its number is NULL rather than 0.0.
"""

import io

import pytest
from PIL import Image

pytestmark = pytest.mark.asyncio

BLR = (12.9716, 77.5946)


def _jpeg():
    b = io.BytesIO()
    Image.new("RGB", (320, 240), (90, 130, 110)).save(b, "JPEG")
    return b.getvalue()


async def _post(client):
    r = await client.post(
        "/sighting",
        files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
        data={"geo_source": "device_gps", "captured_at": "2026-07-19T09:00:00Z",
              "lat": str(BLR[0]), "lng": str(BLR[1]), "geo_accuracy_m": "8.0"},
    )
    assert r.status_code == 201, r.text
    return r.json()["sighting_id"]


async def test_capture_records_a_detection_row_per_photo(authed_client, monkeypatch):
    from types import SimpleNamespace

    from app import analyse as analyse_mod

    monkeypatch.setattr(
        analyse_mod, "analyse",
        lambda raw: SimpleNamespace(dog_confidence=0.42, cat_confidence=0.07,
                                    box=None, image=None, has_animal=False),
    )
    client, _ = authed_client
    sid = await _post(client)

    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT d.model, d.dog, d.cat FROM detections d "
            "JOIN photos p ON p.id = d.photo_id WHERE p.sighting_id = $1", sid)
        conf = await c.fetchval(
            "SELECT animal_confidence FROM sightings WHERE id = $1", sid)

    assert len(rows) == 1
    assert rows[0]["model"] == "yolo26x"
    assert rows[0]["dog"] == pytest.approx(0.42, abs=1e-6)
    assert rows[0]["cat"] == pytest.approx(0.07, abs=1e-6)
    # Any animal counts, so the sighting's number is the max of the two.
    assert conf == pytest.approx(0.42, abs=1e-6)


async def test_a_detector_failure_leaves_the_sighting_visible_and_unscored(
    authed_client, monkeypatch
):
    from app import analyse as analyse_mod

    def boom(raw):
        raise RuntimeError("onnx exploded")

    monkeypatch.setattr(analyse_mod, "analyse", boom)
    client, _ = authed_client
    sid = await _post(client)

    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT animal_confidence, review_status FROM sightings WHERE id = $1", sid)
        n = await c.fetchval(
            "SELECT count(*) FROM detections d JOIN photos p ON p.id = d.photo_id "
            "WHERE p.sighting_id = $1", sid)

    assert row["animal_confidence"] is None, "NULL means never scored, not 0.0"
    assert row["review_status"] == "valid"
    assert n == 0
