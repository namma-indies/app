"""Talking to X, and knowing when we don't know whether a post went out.

The distinction these tests pin is between two failures that look alike: X
saying no (nothing was created, retry tomorrow) and X not answering while a
post was being created (it may exist; never retry blindly). Getting that
wrong is how an account posts the same dog twice.
"""

import io
import json

import httpx
import pytest
from PIL import Image

from app.x_client import XClient, XRejected, XUncertain, prepare_image


def _webp_with_gps(size=(6000, 4000)):
    exif = Image.Exif()
    exif[0x8825] = {2: (12, 58, 17), 4: (77, 35, 40)}  # GPSInfo: lat, lng
    exif[0x010F] = "PhoneMaker"
    b = io.BytesIO()
    Image.new("RGB", size, (120, 90, 60)).save(b, "WEBP", exif=exif.tobytes())
    return b.getvalue()


def test_prepare_image_strips_metadata_and_fits():
    out = prepare_image(_webp_with_gps())
    img = Image.open(io.BytesIO(out))
    assert img.format == "JPEG"
    assert max(img.size) == 4096
    assert len(img.getexif()) == 0
    assert "exif" not in img.info and "xmp" not in img.info
    assert len(out) <= 5 * 1024 * 1024


def test_prepare_image_keeps_small_images_their_size():
    img = Image.open(io.BytesIO(prepare_image(_webp_with_gps((800, 600)))))
    assert img.size == (800, 600)


def _client(handler):
    return XClient("ck", "cs", "at", "ats", transport=httpx.MockTransport(handler))


async def test_upload_then_post_sends_oauth_and_media_id():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        assert request.headers["Authorization"].startswith("OAuth ")
        assert 'oauth_consumer_key="ck"' in request.headers["Authorization"]
        assert 'oauth_token="at"' in request.headers["Authorization"]
        if request.url.path == "/2/media/upload":
            body = request.content
            assert b'name="media_category"' in body and b"tweet_image" in body
            assert b'name="media"' in body
            return httpx.Response(200, json={"data": {"id": "m-1", "media_key": "3_1"}})
        if request.url.path == "/2/tweets":
            return httpx.Response(201, json={"data": {"id": "p-9", "text": "t"}})
        return httpx.Response(404)

    client = _client(handler)
    media_id = await client.upload_image(b"\xff\xd8jpeg")
    post_id = await client.create_post("Spotted in Indiranagar 📸 @a_handle", media_id)
    assert (media_id, post_id) == ("m-1", "p-9")
    assert [r.url.host for r in seen] == ["api.x.com", "api.x.com"]
    assert json.loads(seen[1].content) == {
        "text": "Spotted in Indiranagar 📸 @a_handle", "media": {"media_ids": ["m-1"]}}


@pytest.mark.parametrize("path", ["/2/media/upload", "/2/tweets"])
async def test_http_error_raises_rejected(path):
    def handler(request):
        if request.url.path == path:
            return httpx.Response(403, json={"title": "Forbidden", "detail": "no credits"})
        if request.url.path == "/2/media/upload":
            return httpx.Response(200, json={"data": {"id": "m"}})
        return httpx.Response(201, json={"data": {"id": "p"}})

    client = _client(handler)
    with pytest.raises(XRejected, match="403"):
        await client.create_post("t", await client.upload_image(b"x"))


async def test_timeout_on_create_raises_uncertain_but_on_upload_rejected():
    def times_out_on(path):
        def handler(request):
            if request.url.path == path:
                raise httpx.ReadTimeout("slow", request=request)
            if request.url.path == "/2/media/upload":
                return httpx.Response(200, json={"data": {"id": "m"}})
            return httpx.Response(201, json={"data": {"id": "p"}})
        return handler

    # Nothing is public until a post exists, so a lost upload is safe to redo.
    with pytest.raises(XRejected):
        await _client(times_out_on("/2/media/upload")).upload_image(b"x")
    with pytest.raises(XUncertain):
        await _client(times_out_on("/2/tweets")).create_post("t", "m")


def test_oauth1_signature_matches_x_published_example():
    """The worked example in X's "Creating a signature" guide, whose result is
    hCtSmYh+iHYCEqBWrE7C7hYmtUk=. (The "Authorizing a request" page shows a
    different signature for the same inputs and does not reproduce; signing
    the guide's own base string independently gives the value below.) If this
    ever drifts, every request is rejected with 401 -- better to learn it here."""
    from app.x_client import oauth1_header

    header = oauth1_header(
        "POST", "https://api.twitter.com/1.1/statuses/update.json?include_entities=true",
        consumer_key="xvz1evFS4wEEPTGEFPHBog",
        consumer_secret="kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
        token="370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
        token_secret="LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
        body_params={"status": "Hello Ladies + Gentlemen, a signed OAuth request!"},
        nonce="kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg", timestamp="1318622958",
    )
    assert 'oauth_signature="hCtSmYh%2BiHYCEqBWrE7C7hYmtUk%3D"' in header
    assert header.startswith("OAuth ")


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_server_error_on_create_is_uncertain(status):
    """A 5xx from X's edge does not mean nothing was created -- the post can be
    live behind a 503. Treating it as a refusal would repost tomorrow."""
    def handler(request):
        return httpx.Response(status, text="upstream")
    with pytest.raises(XUncertain):
        await _client(handler).create_post("t", "m")


async def test_server_error_on_upload_is_rejected():
    # Nothing is public until a post exists, so a failed upload is safe to redo.
    with pytest.raises(XRejected):
        await _client(lambda r: httpx.Response(503)).upload_image(b"x")


async def test_rate_limit_on_create_is_rejected():
    with pytest.raises(XRejected, match="429"):
        await _client(lambda r: httpx.Response(429)).create_post("t", "m")
