"""Offering a sighting to the Namma Indies team.

Sharing only means something for a contributor who has consented to being
posted and has a handle to be credited by. For anyone else the option is not
shown, and a request that sends it anyway is ignored rather than stored -- a
share nobody can act on is a promise the app would not keep.
"""

import io
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import UploadFile
from PIL import Image

from app.capture_jobs import complete_capture_job
from app.config import settings
from app.ids import uuid7
from app.security import issue_session
from tests import social_fixtures as fx
from tests.test_media_jobs import FakePool
from tests.test_multi_captures import seed, tracked


def _jpeg():
    b = io.BytesIO()
    Image.new("RGB", (320, 240), (90, 130, 110)).save(b, "JPEG")
    return b.getvalue()


def _pool(client):
    return client._transport.app.state.pool


async def _as(client, **kw):
    async with _pool(client).acquire() as c:
        oid = await fx.observer(c, **kw)
    client.cookies.set("session", issue_session(oid))
    return oid


async def _post_sighting(client, **extra):
    r = await client.post(
        "/sighting",
        files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
        data={"geo_source": "device_gps", "captured_at": "2026-10-24T09:00:00Z",
              "lat": "12.97", "lng": "77.59", "geo_accuracy_m": "8", **extra})
    assert r.status_code == 201, r.text
    return r.json()["sighting_id"]


async def _shared_at(client, sid):
    async with _pool(client).acquire() as c:
        return await c.fetchval("SELECT shared_with_team_at FROM sightings WHERE id=$1::uuid", sid)


async def test_me_reports_can_share(app_client):
    await _as(app_client)
    assert (await app_client.get("/me")).json()["can_share_socially"] is True
    await _as(app_client, consent=False)
    assert (await app_client.get("/me")).json()["can_share_socially"] is False
    await _as(app_client, handle=None)
    assert (await app_client.get("/me")).json()["can_share_socially"] is False


async def test_post_sighting_share_flag_respected_only_with_consent(app_client):
    await _as(app_client)
    assert await _shared_at(app_client, await _post_sighting(app_client, share_with_team="true"))
    assert await _shared_at(app_client, await _post_sighting(app_client)) is None
    await _as(app_client, consent=False)
    assert await _shared_at(app_client, await _post_sighting(app_client, share_with_team="true")) is None


async def test_capture_share_flag_carries_to_published_sightings(migrated_db, monkeypatch):
    from app.routes.capture import create_capture

    conn = migrated_db
    oid = await fx.observer(conn)
    monkeypatch.setattr(settings, "multi_animal_enabled", True)
    monkeypatch.setattr(settings, "media_jobs_enabled", True)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=FakePool(conn))))
    received = await create_capture(
        request=request, photos=None, video=UploadFile(io.BytesIO(b"video")), lat=None,
        lng=None, geo_accuracy_m=None, client_token="t", geo_source="none",
        captured_at=datetime.now(timezone.utc), reported_at=None, note=None,
        share_with_team=True, observer_id=oid, storage=SimpleNamespace(put=AsyncMock()))
    attrs = await conn.fetchval("SELECT attrs->>'share_with_team' FROM captures WHERE id=$1",
                                received["capture_id"])
    assert attrs == "true"

    # Publication is where the capture's sightings come into being, so that is
    # where the share has to land on them.
    body = tracked(("dog",))
    sharer, cid, jid, storage = await seed(conn, body)
    await conn.execute("UPDATE observers SET social_consent=true, x_handle='a' WHERE id=$1", sharer)
    await conn.execute(
        "UPDATE captures SET attrs = attrs || '{\"share_with_team\": true}' WHERE id=$1", cid)
    assert (await complete_capture_job(conn, storage, jid, body))["outcome"] == "ready"
    assert await conn.fetchval(
        "SELECT shared_with_team_at IS NOT NULL FROM sightings WHERE capture_id=$1", cid)


async def test_capture_share_ignored_without_consent(migrated_db):
    conn = migrated_db
    body = tracked(("dog",))
    _, cid, jid, storage = await seed(conn, body)
    await conn.execute(
        "UPDATE captures SET attrs = attrs || '{\"share_with_team\": true}' WHERE id=$1", cid)
    await complete_capture_job(conn, storage, jid, body)
    assert not await conn.fetchval(
        "SELECT shared_with_team_at IS NOT NULL FROM sightings WHERE capture_id=$1", cid)


async def test_share_toggle_owner_only_and_consent_only(app_client):
    owner = await _as(app_client)
    sid = await _post_sighting(app_client)

    r = await app_client.post(f"/sighting/{sid}/share", json={"shared": True})
    assert r.status_code == 200 and r.json() == {"shared": True}
    assert await _shared_at(app_client, sid)

    await _as(app_client, name="Someone else")
    assert (await app_client.post(f"/sighting/{sid}/share", json={"shared": False})).status_code == 403

    async with _pool(app_client).acquire() as c:
        await c.execute("UPDATE observers SET social_consent=false WHERE id=$1", owner)
    app_client.cookies.set("session", issue_session(owner))
    assert (await app_client.post(f"/sighting/{sid}/share", json={"shared": True})).status_code == 403

    assert (await app_client.post(f"/sighting/{uuid7()}/share", json={"shared": True})).status_code == 404


async def test_unshare_removes_queued_pick_keeps_posted(app_client):
    await _as(app_client)
    queued = await _post_sighting(app_client, share_with_team="true")
    posted = await _post_sighting(app_client, share_with_team="true")
    async with _pool(app_client).acquire() as c:
        for sid, status, day in ((queued, "queued", None), (posted, "posted", date(2026, 10, 24))):
            pid = await c.fetchval("SELECT id FROM photos WHERE sighting_id=$1::uuid", sid)
            await c.execute(
                "INSERT INTO social_picks (id, sighting_id, photo_id, caption, status, posted_on) "
                "VALUES ($1,$2::uuid,$3,'c',$4,$5)", uuid7(), sid, pid, status, day)

    for sid in (queued, posted):
        r = await app_client.post(f"/sighting/{sid}/share", json={"shared": False})
        assert r.status_code == 200 and r.json() == {"shared": False}
        assert await _shared_at(app_client, sid) is None

    async with _pool(app_client).acquire() as c:
        left = await c.fetch("SELECT sighting_id::text AS s, status FROM social_picks")
    assert [(r["s"], r["status"]) for r in left] == [(posted, "posted")]


async def test_dex_reports_shared_state(app_client):
    await _as(app_client)
    shared = await _post_sighting(app_client, share_with_team="true")
    plain = await _post_sighting(app_client)
    items = {s["id"]: s for s in (await app_client.get("/dex")).json()["sightings"]}
    assert items[shared]["shared_with_team"] is True
    assert items[plain]["shared_with_team"] is False


async def test_editing_a_published_capture_does_not_reshare(migrated_db):
    """Editing an animal's details re-runs publication. A share the contributor
    withdrew must stay withdrawn -- otherwise the sighting reappears on the
    moderators' list, or a posted one loses its take-down flag."""
    from app.capture_contracts import AnimalGroup
    from app.captures import publish_groups
    from app.social import unshare

    conn = migrated_db
    body = tracked(("dog",))
    sharer, cid, jid, storage = await seed(conn, body)
    await conn.execute("UPDATE observers SET social_consent=true, x_handle='a' WHERE id=$1", sharer)
    await conn.execute(
        "UPDATE captures SET attrs = attrs || '{\"share_with_team\": true}' WHERE id=$1", cid)
    await complete_capture_job(conn, storage, jid, body)
    sid = await conn.fetchval("SELECT id FROM sightings WHERE capture_id=$1", cid)
    await unshare(conn, sid)

    capture = await conn.fetchrow("SELECT * FROM captures WHERE id=$1", cid)
    ids = [r["id"] for r in await conn.fetch(
        "SELECT id FROM animal_instances WHERE sighting_id=$1", sid)]
    await publish_groups(conn, capture, [AnimalGroup(instance_ids=ids, condition="injured")])
    assert await conn.fetchval("SELECT shared_with_team_at FROM sightings WHERE id=$1", sid) is None
