"""Bad uploads must fail in a way the client can act on.

Both cases here were found by walking the API as a user would. Neither was
caught by the existing suite, which only ever posted well-formed input.
"""

import io

import pytest
from PIL import Image


def _jpeg():
    b = io.BytesIO()
    Image.new("RGB", (320, 240), (90, 130, 110)).save(b, "JPEG")
    return b.getvalue()


BASE = {"geo_source": "device_gps", "captured_at": "2026-08-31T09:00:00Z",
        "lat": "12.9716", "lng": "77.5946"}


@pytest.mark.asyncio
async def test_an_unreadable_photo_is_a_422_not_a_500(authed_client):
    """A 500 here is not just untidy. The offline queue classifies 4xx as
    permanent and 5xx as retryable, and its drain *breaks* on a retryable
    failure -- so one corrupt photo stopped the whole queue and every sighting
    behind it never synced, on every pass. 422 lets the queue set it aside and
    carry on."""
    client, _ = authed_client
    r = await client.post(
        "/sighting",
        files={"photos": ("d.jpg", b"this is not an image", "image/jpeg")},
        data=BASE,
    )
    assert r.status_code == 422, f"got {r.status_code}: {r.text[:200]}"


@pytest.mark.asyncio
async def test_a_truncated_photo_is_also_422(authed_client):
    """The likelier real-world shape: a real JPEG cut short mid-write."""
    client, _ = authed_client
    whole = _jpeg()
    r = await client.post(
        "/sighting",
        files={"photos": ("d.jpg", whole[: len(whole) // 3], "image/jpeg")},
        data=BASE,
    )
    assert r.status_code == 422, f"got {r.status_code}: {r.text[:200]}"


@pytest.mark.asyncio
async def test_one_bad_photo_does_not_take_the_good_ones_with_it(authed_client):
    """A multi-photo post with one unreadable file is rejected whole rather
    than half-stored -- the sighting is one observation, and a partial save
    would leave a record whose evidence is missing without saying so."""
    client, _ = authed_client
    r = await client.post(
        "/sighting",
        files=[
            ("photos", ("a.jpg", _jpeg(), "image/jpeg")),
            ("photos", ("b.jpg", b"garbage", "image/jpeg")),
        ],
        data=BASE,
    )
    assert r.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("lat,lng", [(999, 77.59), (-91, 77.59), (12.97, 181),
                                     (12.97, -181), (90.001, 0)])
async def test_impossible_coordinates_are_rejected(authed_client, lat, lng):
    """geography(Point,4326) does not reject an out-of-range latitude, it wraps
    it: lat=999 was stored as -81.0, a real place in Antarctica, and drawn on
    the map like any other pin. A rejection is recoverable; a silent relocation
    is not."""
    client, _ = authed_client
    r = await client.post(
        "/sighting",
        files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
        data={**BASE, "lat": str(lat), "lng": str(lng)},
    )
    assert r.status_code == 422, f"lat={lat} lng={lng} gave {r.status_code}"


@pytest.mark.asyncio
@pytest.mark.parametrize("lat,lng", [(90, 180), (-90, -180), (0, 0), (12.97, 77.59)])
async def test_the_edges_of_the_globe_are_still_valid(authed_client, lat, lng):
    """The poles and the antimeridian are real places. A range check that
    rejects them would break the map at exactly the points a naive one gets
    wrong."""
    client, _ = authed_client
    r = await client.post(
        "/sighting",
        files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
        data={**BASE, "lat": str(lat), "lng": str(lng)},
    )
    assert r.status_code == 201, f"lat={lat} lng={lng} gave {r.status_code}"


@pytest.mark.asyncio
async def test_negative_accuracy_is_rejected(authed_client):
    """A negative radius is not a smaller error, it is a malformed one."""
    client, _ = authed_client
    r = await client.post(
        "/sighting",
        files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
        data={**BASE, "geo_accuracy_m": "-5"},
    )
    assert r.status_code == 422
