import io

import pytest
from PIL import Image


def _jpeg():
    b = io.BytesIO()
    Image.new("RGB", (320, 240), (90, 130, 110)).save(b, "JPEG")
    return b.getvalue()


async def _post(client, geo=True):
    data = {"geo_source": "device_gps" if geo else "none", "captured_at": "2026-07-19T10:00:00Z"}
    if geo:
        data |= {"lat": "12.97", "lng": "77.59", "geo_accuracy_m": "8.0"}
    r = await client.post("/sighting", files={"photos": ("d.jpg", _jpeg(), "image/jpeg")}, data=data)
    assert r.status_code == 201
    return r.json()["sighting_id"]


@pytest.mark.asyncio
async def test_dex_requires_auth(app_client):
    r = await app_client.get("/dex")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_dex_returns_only_own_sightings(authed_client):
    client, oid_a = authed_client
    # observer A posts two
    a1 = await _post(client)
    a2 = await _post(client)
    # create observer B, switch the session cookie, post one as B
    from app.ids import uuid7
    from app.security import issue_session

    pool = client._transport.app.state.pool  # match E2's accessor
    oid_b = uuid7()
    async with pool.acquire() as c:
        await c.execute("INSERT INTO observers (id, display_name, created_via) VALUES ($1,'B','test')", oid_b)
    client.cookies.set("session", issue_session(oid_b))
    b1 = await _post(client)
    # back to A
    client.cookies.set("session", issue_session(oid_a))
    r = await client.get("/dex")
    assert r.status_code == 200
    ids = [s["id"] for s in r.json()["sightings"]]
    assert set(ids) == {a1, a2}
    assert b1 not in ids
    # photos carry presigned urls
    s0 = r.json()["sightings"][0]
    photo = s0["photos"][0]
    assert photo["url"].startswith("http")
    assert photo["thumb_url"].startswith("http")
    # Regression: the thumbnail key was derived with a .jpg string replace,
    # a no-op on .webp keys, so thumb_url silently pointed at the
    # full-resolution original. Asserting "starts with http" passed throughout.
    assert "_thumb.webp" in photo["thumb_url"]
    assert photo["thumb_url"].split("?")[0] != photo["url"].split("?")[0]


@pytest.mark.asyncio
async def test_dex_says_why_a_sighting_is_off_the_shared_map(authed_client, monkeypatch):
    """Yours stays in your dex whatever happens to it -- but a sighting that
    quietly stops appearing on the shared map with no explanation anywhere is
    the bad experience #67 set out to avoid."""
    from app.config import settings

    monkeypatch.setattr(settings, "animal_confidence_min", 0.30)
    client, oid = authed_client
    sid = await _post(client)  # reuse this file's existing helper

    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        await c.execute(
            "UPDATE sightings SET animal_confidence = 0.02 WHERE id = $1", sid)

    item = (await client.get("/dex")).json()["sightings"][0]
    assert item["id"] == str(sid)
    assert item["on_map"] is False
    assert item["off_map_reason"] == "no_animal"


@pytest.mark.asyncio
async def test_a_report_outranks_the_detector_in_the_explanation(authed_client, monkeypatch):
    """Both can be true at once. A person acting is the more useful thing to
    be told, so it is the reason reported."""
    from app.config import settings

    # Without this, animal_confidence_min stays 0.0 and 0.02 >= 0.0 is True --
    # animal_ok would be True and there would be nothing for `pending` to
    # outrank. Mirrors test_dex_says_why_a_sighting_is_off_the_shared_map above.
    monkeypatch.setattr(settings, "animal_confidence_min", 0.30)
    client, oid = authed_client
    sid = await _post(client)
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        await c.execute(
            "UPDATE sightings SET review_status='pending', animal_confidence=0.02 "
            "WHERE id=$1", sid)

    item = (await client.get("/dex")).json()["sightings"][0]
    assert item["off_map_reason"] == "reported"


@pytest.mark.asyncio
async def test_a_moderator_hiding_it_is_reported_as_hidden(authed_client):
    """The third off_map_reason value, otherwise unverified server-side."""
    client, oid = authed_client
    sid = await _post(client)
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        await c.execute(
            "UPDATE sightings SET review_status='rejected' WHERE id=$1", sid)

    item = (await client.get("/dex")).json()["sightings"][0]
    assert item["on_map"] is False
    assert item["off_map_reason"] == "hidden"


@pytest.mark.asyncio
async def test_a_moderator_hiding_it_wins_over_no_animal_too(authed_client, monkeypatch):
    """rejected + a failing animal score: hidden still wins -- the same
    precedence as the pending case, checked against the other losing branch."""
    from app.config import settings

    monkeypatch.setattr(settings, "animal_confidence_min", 0.30)
    client, oid = authed_client
    sid = await _post(client)
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        await c.execute(
            "UPDATE sightings SET review_status='rejected', animal_confidence=0.02 "
            "WHERE id=$1", sid)

    item = (await client.get("/dex")).json()["sightings"][0]
    assert item["off_map_reason"] == "hidden"


@pytest.mark.asyncio
async def test_a_normal_sighting_is_on_the_map(authed_client):
    client, oid = authed_client
    await _post(client)
    item = (await client.get("/dex")).json()["sightings"][0]
    assert item["on_map"] is True
    assert item["off_map_reason"] is None
