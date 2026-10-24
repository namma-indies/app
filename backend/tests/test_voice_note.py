"""A voice note kept with a photo sighting or capture (issue #96)."""
import io

import pytest
from PIL import Image

from app.config import settings
from app.deps import get_storage

# Not a real recording: nothing decodes the note, it is stored as sent.
NOTE = b"\x00\x00\x00\x18ftypM4A voice note bytes"


def _jpeg():
    b = io.BytesIO()
    Image.new("RGB", (400, 300), (100, 140, 90)).save(b, "JPEG")
    return b.getvalue()


def _form():
    return {"geo_source": "none", "captured_at": "2026-10-24T10:00:00Z"}


@pytest.mark.asyncio
async def test_a_photo_sighting_keeps_its_voice_note(authed_client):
    client, _ = authed_client
    r = await client.post(
        "/sighting",
        files=[
            ("photos", ("d.jpg", _jpeg(), "image/jpeg")),
            ("audio", ("note.m4a", NOTE, "audio/mp4")),
        ],
        data=_form(),
    )
    assert r.status_code == 201, r.text
    sid = r.json()["sighting_id"]
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        key = await c.fetchval("SELECT audio_s3_key FROM sightings WHERE id=$1", sid)
    assert key == f"sightings/{sid}/audio.m4a"
    assert await get_storage().get(key) == NOTE

    item = (await client.get("/dex")).json()["sightings"][0]
    assert item["audio_url"] and "audio.m4a" in item["audio_url"]


@pytest.mark.asyncio
async def test_codec_parameters_do_not_change_the_stored_type(authed_client):
    """Android's MediaRecorder labels its output `audio/webm;codecs=opus`."""
    client, _ = authed_client
    r = await client.post(
        "/sighting",
        files=[
            ("photos", ("d.jpg", _jpeg(), "image/jpeg")),
            ("audio", ("note.webm", NOTE, "audio/webm;codecs=opus")),
        ],
        data=_form(),
    )
    assert r.status_code == 201, r.text
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        key = await c.fetchval(
            "SELECT audio_s3_key FROM sightings WHERE id=$1", r.json()["sighting_id"])
    assert key.endswith("/audio.webm")


@pytest.mark.asyncio
async def test_a_sighting_without_a_note_is_unchanged(authed_client):
    client, _ = authed_client
    r = await client.post(
        "/sighting", files={"photos": ("d.jpg", _jpeg(), "image/jpeg")}, data=_form())
    assert r.status_code == 201
    item = (await client.get("/dex")).json()["sightings"][0]
    assert item["audio_url"] is None


@pytest.mark.asyncio
async def test_a_clip_carries_no_separate_note(authed_client):
    client, _ = authed_client
    r = await client.post(
        "/sighting",
        files=[
            ("video", ("clip.mp4", b"not decoded", "video/mp4")),
            ("audio", ("note.m4a", NOTE, "audio/mp4")),
        ],
        data=_form(),
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_a_note_that_is_not_audio_is_refused(authed_client):
    client, _ = authed_client
    r = await client.post(
        "/sighting",
        files=[
            ("photos", ("d.jpg", _jpeg(), "image/jpeg")),
            ("audio", ("note.txt", NOTE, "text/plain")),
        ],
        data=_form(),
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_an_oversized_note_is_refused(authed_client, monkeypatch):
    client, _ = authed_client
    monkeypatch.setattr(settings, "media_max_audio_bytes", 8)
    r = await client.post(
        "/sighting",
        files=[
            ("photos", ("d.jpg", _jpeg(), "image/jpeg")),
            ("audio", ("note.m4a", NOTE, "audio/mp4")),
        ],
        data=_form(),
    )
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_a_capture_keeps_its_voice_note(authed_client, monkeypatch):
    client, _ = authed_client
    monkeypatch.setattr(settings, "multi_animal_enabled", True)
    monkeypatch.setattr(settings, "media_jobs_enabled", True)
    r = await client.post(
        "/capture",
        files=[
            ("photos", ("d.jpg", _jpeg(), "image/jpeg")),
            ("audio", ("note.m4a", NOTE, "audio/mp4")),
        ],
        data=_form(),
    )
    assert r.status_code == 201, r.text
    cid = r.json()["capture_id"]
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        key = await c.fetchval("SELECT audio_s3_key FROM captures WHERE id=$1", cid)
    assert key == f"captures/{cid}/audio.m4a"
    assert await get_storage().get(key) == NOTE
