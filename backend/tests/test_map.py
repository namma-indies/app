"""The cohort-wide map.

`/dex` means "my dex" -- it feeds the grid and its `WHERE observer_id = $1` is
the ownership semantics `resolve_sighting` and the future standing model both
read. The map wants a different thing (everyone's sightings) with a different
payload (thumbnails only), so it gets its own endpoint rather than a scope flag
that muddies what `/dex` means.

One request serves both sides of the Mine/Everyone toggle: each sighting says
whether it is the viewer's, so flipping the toggle is a client-side filter
rather than a round trip.

Visibility is the whole authenticated cohort at full precision. That is safe
today because the cohort is passcode- and allowlist-gated. Issue #5's
`resolve_precision` coarsening is what a *public* map would need, and is
deliberately not built here.
"""

import io

import pytest
from PIL import Image


def _jpeg(colour=(90, 130, 110)):
    b = io.BytesIO()
    Image.new("RGB", (320, 240), colour).save(b, "JPEG")
    return b.getvalue()


async def _post(client, *, lat="12.97", lng="77.59", note=None):
    data = {
        "geo_source": "device_gps",
        "captured_at": "2026-07-19T10:00:00Z",
        "lat": lat,
        "lng": lng,
        "geo_accuracy_m": "8.0",
    }
    if note:
        data["note"] = note
    r = await client.post(
        "/sighting", files={"photos": ("d.jpg", _jpeg(), "image/jpeg")}, data=data
    )
    assert r.status_code == 201, r.text
    return r.json()["sighting_id"]


async def _second_observer(client, name="Aswin"):
    """Creates another observer and switches the session cookie to them.
    Returns their id; the caller restores the cookie with `_become`."""
    from app.ids import uuid7
    from app.security import issue_session

    pool = client._transport.app.state.pool
    oid = uuid7()
    async with pool.acquire() as c:
        await c.execute(
            "INSERT INTO observers (id, display_name, created_via) VALUES ($1,$2,'test')",
            oid,
            name,
        )
    client.cookies.set("session", issue_session(oid))
    return oid


def _become(client, oid):
    from app.security import issue_session

    client.cookies.set("session", issue_session(oid))


@pytest.mark.asyncio
async def test_map_requires_auth(app_client):
    assert (await app_client.get("/map")).status_code == 401


@pytest.mark.asyncio
async def test_map_returns_every_observers_sightings(authed_client):
    client, mine_oid = authed_client
    a = await _post(client)
    await _second_observer(client)
    b = await _post(client)
    _become(client, mine_oid)

    r = await client.get("/map")
    assert r.status_code == 200
    ids = {s["id"] for s in r.json()["sightings"]}
    assert ids == {a, b}, "the map is the cohort's, not the viewer's"


@pytest.mark.asyncio
async def test_each_sighting_says_whether_it_is_mine(authed_client):
    """This is what makes the Mine/Everyone toggle a filter, not a fetch."""
    client, mine_oid = authed_client
    mine = await _post(client)
    await _second_observer(client)
    theirs = await _post(client)
    _become(client, mine_oid)

    by_id = {s["id"]: s for s in (await client.get("/map")).json()["sightings"]}
    assert by_id[mine]["mine"] is True
    assert by_id[theirs]["mine"] is False


@pytest.mark.asyncio
async def test_attribution_is_present_for_the_popup(authed_client):
    """Akash's call: who logged it shows on tapping a sighting, not on the pin.
    The name has to be in the payload either way."""
    client, mine_oid = authed_client
    await _second_observer(client, name="Aswin")
    theirs = await _post(client)
    _become(client, mine_oid)

    by_id = {s["id"]: s for s in (await client.get("/map")).json()["sightings"]}
    assert by_id[theirs]["observer"] == "Aswin"


@pytest.mark.asyncio
async def test_payload_carries_thumbnails_and_not_originals(authed_client):
    """The reason this is not `/dex` with a flag. A cohort map on Indian mobile
    data cannot ship a full-resolution WebP per pin."""
    client, _ = authed_client
    await _post(client)

    photo = (await client.get("/map")).json()["sightings"][0]["photos"][0]
    assert "_thumb.webp" in photo["thumb_url"]
    assert "url" not in photo, "originals have no business in a map payload"


@pytest.mark.asyncio
async def test_sightings_without_coordinates_are_omitted(authed_client):
    """A pin needs a point. An offline capture with geo_source=none has none."""
    client, _ = authed_client
    located = await _post(client)
    r = await client.post(
        "/sighting",
        files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
        data={"geo_source": "none", "captured_at": "2026-07-19T10:00:00Z"},
    )
    assert r.status_code == 201
    unlocated = r.json()["sighting_id"]

    ids = {s["id"] for s in (await client.get("/map")).json()["sightings"]}
    assert located in ids
    assert unlocated not in ids


@pytest.mark.asyncio
async def test_bbox_filters_to_the_viewport(authed_client):
    client, _ = authed_client
    blr = await _post(client, lat="12.97", lng="77.59")
    delhi = await _post(client, lat="28.61", lng="77.21")

    r = await client.get("/map", params={"bbox": "77.4,12.8,77.8,13.1"})
    ids = {s["id"] for s in r.json()["sightings"]}
    assert blr in ids
    assert delhi not in ids


@pytest.mark.asyncio
async def test_malformed_bbox_is_a_422_not_a_silent_full_scan(authed_client):
    client, _ = authed_client
    await _post(client)
    for bad in ("1,2,3", "a,b,c,d", "77.8,12.8,77.4,13.1", ""):
        r = await client.get("/map", params={"bbox": bad})
        assert r.status_code == 422, f"{bad!r} should be rejected, got {r.status_code}"


@pytest.mark.asyncio
async def test_limit_is_capped(authed_client):
    client, _ = authed_client
    await _post(client)
    assert (await client.get("/map", params={"limit": "999999"})).status_code == 422
    assert (await client.get("/map", params={"limit": "0"})).status_code == 422


@pytest.mark.asyncio
async def test_newest_first(authed_client):
    client, _ = authed_client
    for day in ("01", "02", "03"):
        r = await client.post(
            "/sighting",
            files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
            data={
                "geo_source": "device_gps",
                "captured_at": f"2026-07-{day}T10:00:00Z",
                "lat": "12.97",
                "lng": "77.59",
            },
        )
        assert r.status_code == 201

    days = [s["captured_at"][:10] for s in (await client.get("/map")).json()["sightings"]]
    assert days == sorted(days, reverse=True)


@pytest.mark.asyncio
async def test_one_row_per_sighting_not_per_photo(authed_client):
    """A multi-photo sighting (a video clip yields up to twelve frames) is one
    pin. The join must not multiply it."""
    client, _ = authed_client
    r = await client.post(
        "/sighting",
        files=[
            ("photos", ("a.jpg", _jpeg((10, 20, 30)), "image/jpeg")),
            ("photos", ("b.jpg", _jpeg((200, 40, 60)), "image/jpeg")),
        ],
        data={
            "geo_source": "device_gps",
            "captured_at": "2026-07-19T10:00:00Z",
            "lat": "12.97",
            "lng": "77.59",
        },
    )
    assert r.status_code == 201

    sightings = (await client.get("/map")).json()["sightings"]
    assert len(sightings) == 1
    assert len(sightings[0]["photos"]) == 1, "the map needs one thumbnail, not all of them"


@pytest.mark.asyncio
async def test_the_thumbnail_choice_is_deterministic(authed_client):
    """Which of a sighting's photos becomes its pin must not be left to the plan.

    Every photo of a sighting is inserted in one transaction, and Postgres holds
    now() constant across it, so they all carry an identical created_at
    (measured: 22 of 22 multi-photo sightings in a dev corpus). Ordering on
    created_at alone leaves the winner to whatever plan Postgres picks, so a
    video sighting's pin can show a different frame on each load.

    Note that asserting "two calls agree" does NOT catch this -- a small table
    is returned in stable physical order, and that version of the test passed
    with the bug still present. So this inserts a second photo whose id sorts
    *below* the first while lying physically after it: the two orderings then
    disagree about the answer, and only the tiebreak gives the low id.
    """
    from uuid import UUID

    client, _ = authed_client
    pool = client._transport.app.state.pool
    r = await client.post(
        "/sighting",
        files={"photos": ("a.jpg", _jpeg(), "image/jpeg")},
        data={
            "geo_source": "device_gps",
            "captured_at": "2026-07-19T10:00:00Z",
            "lat": "12.97",
            "lng": "77.59",
        },
    )
    assert r.status_code == 201
    sid = r.json()["sighting_id"]

    async with pool.acquire() as c:
        created_at = await c.fetchval(
            "SELECT created_at FROM photos WHERE sighting_id = $1::uuid", sid)
        await c.execute(
            "INSERT INTO photos (id, sighting_id, s3_key, created_at) "
            "VALUES ($1, $2::uuid, $3, $4)",
            UUID(int=1),  # sorts below every uuid7
            sid,
            "sightings/x/aaa-lowest.webp",
            created_at,
        )

    url = (await client.get("/map")).json()["sightings"][0]["photos"][0]["thumb_url"]
    assert "aaa-lowest_thumb.webp" in url
