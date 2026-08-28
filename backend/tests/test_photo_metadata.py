"""The camera-roll import preflight.

The client sends the *head* of a chosen file and learns what the photo already
knows about itself, so it can prompt for only the missing half. EXIF's APP1
segment is capped at 64KB by the spec and sits at the front of the file, so a
128KB slice always contains it -- the preflight costs a fraction of the photo
rather than a second full upload.

The prompt exists because no-EXIF is the common case, not the edge: WhatsApp
forwards, screenshots and several gallery apps strip it entirely. The
alternative to asking is defaulting to here-and-now, which silently inserts a
phantom sighting into the 1km spatial prior `resolve_sighting` matches against.
"""

import io

import piexif
import pytest
from PIL import Image

from tests.test_exif import _exif, _jpeg_with_exif


@pytest.mark.asyncio
async def test_requires_auth(app_client):
    r = await app_client.post(
        "/photo/metadata", files={"head": ("d.jpg", _jpeg_with_exif(_exif()), "image/jpeg")}
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_returns_date_and_location(authed_client):
    client, _ = authed_client
    raw = _jpeg_with_exif(
        _exif(date="2026:08:05 18:42:11", offset="+05:30", lat=12.9352, lng=77.6245)
    )
    r = await client.post("/photo/metadata", files={"head": ("d.jpg", raw, "image/jpeg")})
    assert r.status_code == 200
    body = r.json()
    assert body["captured_at_local"] == "2026-08-05T18:42:11"
    assert body["utc_offset_minutes"] == 330
    assert body["lat"] == pytest.approx(12.9352, abs=1e-5)
    assert body["lng"] == pytest.approx(77.6245, abs=1e-5)
    assert body["has_date"] is True
    assert body["has_location"] is True


@pytest.mark.asyncio
async def test_reports_what_is_missing_so_the_client_can_prompt(authed_client):
    client, _ = authed_client
    raw = _jpeg_with_exif(_exif(date="2026:08:05 18:42:11"))
    r = await client.post("/photo/metadata", files={"head": ("d.jpg", raw, "image/jpeg")})
    body = r.json()
    assert body["has_date"] is True
    assert body["has_location"] is False
    assert body["lat"] is None and body["lng"] is None


@pytest.mark.asyncio
async def test_no_metadata_is_200_not_an_error(authed_client):
    """A stripped photo is a normal thing to import, not a failure. The client
    prompts for both fields; a 4xx would push it down an error path instead."""
    client, _ = authed_client
    img = Image.new("RGB", (32, 32))
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    r = await client.post(
        "/photo/metadata", files={"head": ("d.jpg", buf.getvalue(), "image/jpeg")}
    )
    assert r.status_code == 200
    assert r.json() == {
        "captured_at_local": None,
        "utc_offset_minutes": None,
        "lat": None,
        "lng": None,
        "has_date": False,
        "has_location": False,
    }


@pytest.mark.asyncio
async def test_non_image_bytes_are_200_with_nothing_found(authed_client):
    """The preflight is a question, not a validation gate -- the real upload
    still rejects undecodable bytes."""
    client, _ = authed_client
    r = await client.post(
        "/photo/metadata", files={"head": ("x.jpg", b"not an image at all", "image/jpeg")}
    )
    assert r.status_code == 200
    assert r.json()["has_date"] is False


@pytest.mark.asyncio
async def test_truncated_head_still_yields_metadata(authed_client):
    """What the client actually sends: file.slice(0, 131072)."""
    client, _ = authed_client
    img = Image.new("RGB", (1600, 1200))
    px = img.load()
    for y in range(0, 1200, 5):
        for x in range(0, 1600, 5):
            px[x, y] = ((x * 7) % 256, (y * 13) % 256, ((x + y) * 3) % 256)
    buf = io.BytesIO()
    img.save(
        buf,
        "JPEG",
        quality=92,
        exif=piexif.dump(_exif(date="2026:08:05 18:42:11", lat=12.9, lng=77.6)),
    )
    raw = buf.getvalue()
    assert len(raw) > 200_000

    r = await client.post(
        "/photo/metadata", files={"head": ("d.jpg", raw[:131_072], "image/jpeg")}
    )
    assert r.status_code == 200
    assert r.json()["captured_at_local"] == "2026-08-05T18:42:11"


@pytest.mark.asyncio
async def test_oversized_head_is_refused(authed_client):
    """A preflight has no business carrying a whole photo. The cap is what
    makes this cheap enough to call on every import."""
    client, _ = authed_client
    r = await client.post(
        "/photo/metadata", files={"head": ("d.jpg", b"\xff\xd8" + b"\x00" * 900_000, "image/jpeg")}
    )
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_only_the_cap_is_read_not_the_whole_stream(authed_client):
    """At the boundary: exactly the cap is accepted."""
    from app.routes.photo_metadata import MAX_HEAD_BYTES

    client, _ = authed_client
    raw = _jpeg_with_exif(_exif(date="2026:08:05 18:42:11"))
    padded = raw + b"\x00" * (MAX_HEAD_BYTES - len(raw))
    assert len(padded) == MAX_HEAD_BYTES
    r = await client.post("/photo/metadata", files={"head": ("d.jpg", padded, "image/jpeg")})
    assert r.status_code == 200
    assert r.json()["captured_at_local"] == "2026-08-05T18:42:11"
