"""The moderators' side of the daily post: what has been shared, picking a
photo and caption, passing, and the queue with its history.

These routes are moderator-only and answer 404 to anyone else, the same as
every other moderation route -- they should not even confirm they exist.
"""

from datetime import date

import pytest

from app.ids import uuid7
from app.security import issue_session
from tests import social_fixtures as fx


def _pool(client):
    return client._transport.app.state.pool


async def _moderator(client):
    async with _pool(client).acquire() as c:
        oid = await fx.observer(c, handle=None, consent=False, tier="moderator", name="Mod")
    client.cookies.set("session", issue_session(oid))
    return oid


async def _shared(client, **kw):
    """A consenting contributor's shared sighting, its photo ids and handle."""
    async with _pool(client).acquire() as c:
        oid = await fx.observer(c, handle=kw.pop("handle", "a_handle"))
        sid, pids = await fx.legacy_sighting(c, oid, **kw)
    return str(sid), [str(p) for p in pids]


async def _pick(client, sid, pid, caption="Spotted in Indiranagar 📸 @a_handle"):
    return await client.post("/social/pick", json={
        "sighting_id": sid, "photo_id": pid, "caption": caption})


@pytest.mark.parametrize("method,path", [
    ("get", "/social/shared"),
    ("get", "/social/queue"),
    ("post", "/social/pick"),
    ("post", "/social/pass"),
    ("delete", f"/social/pick/{uuid7()}"),
])
async def test_social_routes_404_for_non_moderators(app_client, method, path):
    async with _pool(app_client).acquire() as c:
        oid = await fx.observer(c)
    app_client.cookies.set("session", issue_session(oid))
    kw = {"json": {}} if method == "post" else {}
    r = await getattr(app_client, method)(path, **kw)
    assert r.status_code == 404


async def test_shared_lists_only_eligible_unpicked(app_client):
    async with _pool(app_client).acquire() as c:
        await fx.ward(c, "Indiranagar")
    named, named_pids = await _shared(app_client, photos=2)
    unnamed, _ = await _shared(app_client, at=(13.5, 78.5))
    await _shared(app_client, shared=False)
    await _shared(app_client, review="rejected")
    picked, picked_pids = await _shared(app_client)
    await _moderator(app_client)
    assert (await _pick(app_client, picked, picked_pids[0])).status_code == 201

    r = await app_client.get("/social/shared")
    assert r.status_code == 200
    items = {i["sighting_id"]: i for i in r.json()["items"]}
    assert set(items) == {named, unnamed}
    assert items[named]["area"] == "Indiranagar"
    assert items[named]["handle"] == "a_handle"
    assert items[named]["default_caption"] == "Spotted in Indiranagar 📸 @a_handle"
    assert [p["photo_id"] for p in items[named]["photos"]] == named_pids
    assert all(p["url"] and p["thumb_url"] for p in items[named]["photos"])
    assert items[unnamed]["area"] is None
    assert items[unnamed]["default_caption"] == "Spotted in {area} 📸 @a_handle"


@pytest.mark.parametrize("bad", [
    "", "Spotted in {area} 📸 @a", "see https://x.co", "nammaindies.org/app", "x" * 261,
])
async def test_pick_rejects_bad_captions(app_client, bad):
    sid, pids = await _shared(app_client)
    await _moderator(app_client)
    assert (await _pick(app_client, sid, pids[0], bad)).status_code == 422


async def test_pick_refuses_photo_not_postable_for_that_sighting(app_client):
    sid, _ = await _shared(app_client)
    _, other_pids = await _shared(app_client)
    await _moderator(app_client)
    assert (await _pick(app_client, sid, other_pids[0])).status_code == 422


async def test_pick_twice_same_sighting_or_photo_is_409(app_client):
    sid, pids = await _shared(app_client, photos=2)
    await _moderator(app_client)
    assert (await _pick(app_client, sid, pids[0])).status_code == 201
    assert (await _pick(app_client, sid, pids[1])).status_code == 409
    # Two sightings cut from one frame share a source photo; only one may go.
    async with _pool(app_client).acquire() as c:
        oid = await fx.observer(c)
        cid = await c.fetchval("SELECT gen_random_uuid()")
        await c.execute("INSERT INTO captures (id, observer_id, kind, captured_at, processing_state) "
                        "VALUES ($1,$2,'photo',now(),'ready')", cid, oid)
        src = uuid7()
        await c.execute("INSERT INTO photos (id, capture_id, s3_key, content_sha256) "
                        "VALUES ($1,$2,'k','h')", src, cid)
        sids = []
        for n in range(2):
            s, crop = uuid7(), uuid7()
            await c.execute("INSERT INTO sightings (id, capture_id, observer_id, captured_at, "
                            "geo_source, species, shared_with_team_at) "
                            "VALUES ($1,$2,$3,now(),'none','dog',now())", s, cid, oid)
            await c.execute("INSERT INTO photos (id, capture_id, sighting_id, s3_key) "
                            "VALUES ($1,$2,$3,'c')", crop, cid, s)
            await c.execute("""INSERT INTO animal_instances (id, capture_id, source_photo_id,
                evidence_photo_id, sighting_id, track_id, species, confidence, bbox, crop_bbox)
                VALUES ($1,$2,$3,$4,$5,$6,'dog',0.9,'[0,0,1,1]','[0,0,1,1]')""",
                            uuid7(), cid, src, crop, s, f"t{n}")
            sids.append(str(s))
    assert (await _pick(app_client, sids[0], str(src))).status_code == 201
    assert (await _pick(app_client, sids[1], str(src))).status_code == 409


async def test_pass_hides_from_shared(app_client):
    sid, _ = await _shared(app_client)
    await _moderator(app_client)
    assert (await app_client.post("/social/pass", json={"sighting_id": sid})).status_code == 201
    assert (await app_client.get("/social/shared")).json()["items"] == []


async def test_delete_only_unposted(app_client):
    sid, pids = await _shared(app_client)
    posted, posted_pids = await _shared(app_client)
    await _moderator(app_client)
    pick_id = (await _pick(app_client, sid, pids[0])).json()["id"]
    async with _pool(app_client).acquire() as c:
        posted_id = uuid7()
        await c.execute("INSERT INTO social_picks (id, sighting_id, photo_id, caption, status, "
                        "posted_on, x_post_id) VALUES ($1,$2::uuid,$3::uuid,'c','posted',$4,'1')",
                        posted_id, posted, posted_pids[0], date(2026, 10, 24))
    assert (await app_client.delete(f"/social/pick/{posted_id}")).status_code == 409
    assert (await app_client.delete(f"/social/pick/{pick_id}")).status_code == 204
    # Back on the shared list, ready to be picked again.
    assert [i["sighting_id"] for i in (await app_client.get("/social/shared")).json()["items"]] == [sid]
    assert (await app_client.delete(f"/social/pick/{uuid7()}")).status_code == 404


async def test_queue_flags_posted_sighting_later_rejected(app_client):
    queued, qpids = await _shared(app_client)
    kept, kpids = await _shared(app_client)
    gone, gpids = await _shared(app_client)
    await _moderator(app_client)
    assert (await _pick(app_client, queued, qpids[0])).status_code == 201
    async with _pool(app_client).acquire() as c:
        for n, (sid, pid) in enumerate(((kept, kpids[0]), (gone, gpids[0]))):
            await c.execute("INSERT INTO social_picks (id, sighting_id, photo_id, caption, "
                            "status, posted_on, posted_at, x_post_id) "
                            "VALUES ($1,$2::uuid,$3::uuid,'c','posted',$4,now(),$5)",
                            uuid7(), sid, pid, date(2026, 10, 20 + n), f"x{n}")
        await c.execute("UPDATE sightings SET review_status='rejected' WHERE id=$1::uuid", gone)

    q = (await app_client.get("/social/queue")).json()
    assert [r["sighting_id"] for r in q["queued"]] == [queued]
    assert q["queued"][0]["thumb_url"]
    flags = {r["sighting_id"]: r["flagged"] for r in q["posted"]}
    assert flags == {kept: False, gone: True}
    assert q["attention"] == []


async def test_queue_attention_lists_uncertain_and_errored(app_client):
    stuck, spids = await _shared(app_client)
    errored, epids = await _shared(app_client)
    await _moderator(app_client)
    async with _pool(app_client).acquire() as c:
        await c.execute("INSERT INTO social_picks (id, sighting_id, photo_id, caption, status) "
                        "VALUES ($1,$2::uuid,$3::uuid,'c','posting')", uuid7(), stuck, spids[0])
        await c.execute("INSERT INTO social_picks (id, sighting_id, photo_id, caption, status, "
                        "last_error) VALUES ($1,$2::uuid,$3::uuid,'c','queued','X said no')",
                        uuid7(), errored, epids[0])
    q = (await app_client.get("/social/queue")).json()
    assert {r["sighting_id"]: r["status"] for r in q["attention"]} == {
        stuck: "posting", errored: "queued"}


async def test_queue_lists_refused_picks_after_untried_ones(app_client):
    tried, tpids = await _shared(app_client)
    fresh, fpids = await _shared(app_client)
    await _moderator(app_client)
    async with _pool(app_client).acquire() as c:
        await c.execute("INSERT INTO social_picks (id, sighting_id, photo_id, caption, status, "
                        "picked_at, last_attempt_at, last_error) VALUES ($1,$2::uuid,$3::uuid,'c',"
                        "'queued', now() - interval '1 day', now(), 'refused')", uuid7(), tried, tpids[0])
        await c.execute("INSERT INTO social_picks (id, sighting_id, photo_id, caption, status) "
                        "VALUES ($1,$2::uuid,$3::uuid,'c','queued')", uuid7(), fresh, fpids[0])
    q = (await app_client.get("/social/queue")).json()
    assert [r["sighting_id"] for r in q["queued"]] == [fresh, tried]
