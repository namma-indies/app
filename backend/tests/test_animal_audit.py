"""Animal audits expose stored evidence, never inference or publication commands."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.ids import uuid7
from app.routes.animal_audit import _cursor, _serializer
from tests.test_moderation_animals import _moderator, _pool, _scored, _sighting

pytestmark = pytest.mark.asyncio
BASE = "/moderation/animal-audit"


async def _capture(client, state="no_animal", *, photo=True, scores=None, clip=None):
    cid, pid, oid = uuid7(), uuid7(), uuid7()
    async with (await _pool(client)).acquire() as conn:
        await conn.execute("INSERT INTO observers(id) VALUES($1)", oid)
        await conn.execute("""INSERT INTO captures(id,observer_id,kind,captured_at,processing_state,clip_s3_key)
            VALUES($1,$2,'photo',now(),$3,$4)""", cid, oid, state, clip)
        if photo:
            await conn.execute("""INSERT INTO photos(id,capture_id,s3_key,width,height)
                VALUES($1,$2,$3,400,300)""", pid, cid, f"source/{pid}.webp")
            if scores is not None:
                await conn.execute("INSERT INTO detections(photo_id,model,dog,cat) VALUES($1,'yolo26x',$2,$3)", pid, *scores)
    return cid, pid


async def _ids(client, filter="all"):
    response = await client.get(BASE, params={"filter": filter})
    assert response.status_code == 200, response.text
    return {i["id"] for i in response.json()["items"]}


async def _instance(conn, cid, source_id, sid=None, *, species="dog", score=0.8):
    pid, iid = uuid7(), uuid7()
    await conn.execute("""INSERT INTO photos(id,capture_id,sighting_id,s3_key,width,height)
        VALUES($1,$2,$3,$4,120,160)""", pid, cid, sid, f"crop/{pid}.webp")
    await conn.execute("""INSERT INTO animal_instances(id,capture_id,source_photo_id,evidence_photo_id,
        sighting_id,track_id,species,confidence,bbox,crop_bbox)
        VALUES($1,$2,$3,$4,$5,$6,$7,$8,'[110,70,210,210]','[100,60,220,220]')""",
        iid, cid, source_id, pid, sid, str(iid), species, score)
    await conn.execute("INSERT INTO detections(photo_id,model,dog,cat) VALUES($1,'yolo26x',$2,$3)",
        pid, score if species == "dog" else 0, score if species == "cat" else 0)
    return pid


async def test_all_includes_sightings_and_only_unpublished_nonlegacy_captures(app_client):
    await _moderator(app_client)
    sid = await _scored(app_client, dog=0.9, cat=0)
    captures = [await _capture(app_client, state) for state in
                ("queued", "processing", "failed", "needs_review", "no_animal", "ready")]
    published, _ = await _capture(app_client, "ready")
    sibling = uuid7()
    async with (await _pool(app_client)).acquire() as conn:
        await conn.execute("INSERT INTO sightings(id,capture_id,captured_at) VALUES($1,$2,now())", sibling, published)
        await conn.execute("INSERT INTO captures(id,kind,captured_at,processing_state) VALUES($1,'photo',now(),'legacy')", uuid7())
    assert await _ids(app_client) == {str(sid), str(sibling), *(str(c[0]) for c in captures)}
    assert (await app_client.get(f"{BASE}/capture/{published}")).status_code == 404


async def test_filters_keep_missing_detection_human_verdict_and_processing_distinct(app_client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "animal_review_max", 0.6)
    await _moderator(app_client)
    low = await _scored(app_client, dog=0.2, cat=0)
    high = await _scored(app_client, dog=0.9, cat=0)
    boundary = await _scored(app_client, dog=0.6, cat=0)
    missing = await _sighting(app_client, animal_confidence=None, photos=[])
    no_animal, _ = await _capture(app_client, scores=(0.0, 0.0))
    failed, _ = await _capture(app_client, "failed")
    review, _ = await _capture(app_client, "needs_review", scores=(0.99, 0))
    reviewed = await _scored(app_client, dog=0.01, cat=0)
    await app_client.post(f"/sighting/{reviewed}/animal", data={"verdict": "no_animal"})
    assert await _ids(app_client, "needs_review") == {str(x) for x in (low, missing, no_animal, failed, review)}
    assert await _ids(app_client, "reviewed") == {str(reviewed)}
    assert await _ids(app_client, "no_detection") == {str(missing), str(failed), str(no_animal)}
    assert {str(high), str(boundary)} <= await _ids(app_client)
    await app_client.post(f"{BASE}/capture/{no_animal}", data={"verdict": "animal"})
    detail = (await app_client.get(f"{BASE}/capture/{no_animal}")).json()
    assert detail["animal_override"] is True
    assert detail["processing_state"] == "no_animal"
    assert detail["dog"] == 0.0
    assert str(no_animal) not in await _ids(app_client, "needs_review")
    assert str(no_animal) in await _ids(app_client, "reviewed")


async def test_cursor_ties_are_stable_across_kinds_and_changing_filter_membership(app_client):
    await _moderator(app_client)
    capture, _ = await _capture(app_client)
    other, _ = await _capture(app_client)
    # UUIDs are unique per table, not globally; kind must break this exact tie.
    async with (await _pool(app_client)).acquire() as conn:
        await conn.execute("INSERT INTO sightings(id,capture_id,captured_at) VALUES($1,$2,now())", capture, other)
        await conn.execute("UPDATE captures SET created_at='2026-01-01T00:00:00Z'")
        await conn.execute("UPDATE sightings SET created_at='2026-01-01T00:00:00Z'")
    first = (await app_client.get(BASE, params={"limit": 1})).json()
    assert first["items"][0]["kind"] == "sighting"
    assert first["next_cursor"]
    # Ruling on the anchor does not invalidate a cursor.
    await app_client.post(f"/sighting/{capture}/animal", data={"verdict": "animal"})
    second = (await app_client.get(BASE, params={"limit": 1, "before": first["next_cursor"]})).json()
    assert second["items"][0]["kind"] == "capture"
    assert second["items"][0]["id"] == str(capture)
    assert second["next_cursor"] is None
    assert (await app_client.get(BASE, params={"filter": "reviewed", "before": first["next_cursor"]})).status_code == 422


async def test_pagination_reaches_every_row_once(app_client):
    await _moderator(app_client)
    expected = {str((await _capture(app_client))[0]) for _ in range(7)}
    found, cursor = [], None
    for _ in range(4):
        params = {"limit": 2}
        if cursor:
            params["before"] = cursor
        result = (await app_client.get(BASE, params=params)).json()
        found.extend(i["id"] for i in result["items"])
        cursor = result["next_cursor"]
    assert set(found) == expected
    assert len(found) == 7
    assert cursor is None


@pytest.mark.parametrize("params", [
    {"before": "garbage"}, {"before": ""}, {"before": "x" * 2049},
    {"filter": "wat"}, {"limit": 0}, {"limit": 101}, {"limit": "wat"},
])
async def test_invalid_query_is_rejected(app_client, params):
    await _moderator(app_client)
    assert (await app_client.get(BASE, params=params)).status_code == 422


async def test_unknown_valid_cursor_is_a_position_not_a_required_row(app_client):
    await _moderator(app_client)
    cursor = _cursor({"id": uuid7(), "created_at": datetime(2000, 1, 1, tzinfo=timezone.utc), "kind": "capture"}, "all")
    assert (await app_client.get(BASE, params={"before": cursor})).json() == {"items": [], "next_cursor": None}
    for payload in ({}, [], {"v": 2}, {"v": 1, "filter": "all", "kind": "capture", "created_at": "2026-01-01", "id": str(uuid7())}):
        assert (await app_client.get(BASE, params={"before": _serializer().dumps(payload)})).status_code == 422


@pytest.mark.parametrize("method,path", [("GET", BASE), ("GET", BASE + "/capture/{id}"),
    ("GET", BASE + "/sighting/{id}"), ("POST", BASE + "/capture/{id}")])
async def test_all_endpoints_require_live_moderator(app_client, method, path):
    path = path.format(id=uuid7())
    assert (await app_client.request(method, path, data={"verdict": "animal"})).status_code == 401
    oid = await _moderator(app_client)
    async with (await _pool(app_client)).acquire() as conn:
        await conn.execute("UPDATE observers SET trust_tier='new' WHERE id=$1", oid)
    assert (await app_client.request(method, path, data={"verdict": "animal"})).status_code == 404
    async with (await _pool(app_client)).acquire() as conn:
        await conn.execute("UPDATE observers SET trust_tier='moderator',deleted_at=now() WHERE id=$1", oid)
    assert (await app_client.request(method, path, data={"verdict": "animal"})).status_code == 404


async def test_unknown_item_and_invalid_kind_or_verdict(app_client):
    await _moderator(app_client)
    for kind in ("capture", "sighting"):
        assert (await app_client.get(f"{BASE}/{kind}/{uuid7()}")).status_code == 404
    assert (await app_client.get(f"{BASE}/dog/{uuid7()}")).status_code == 422
    assert (await app_client.get(f"{BASE}/capture/not-uuid")).status_code == 422
    assert (await app_client.post(f"{BASE}/capture/{uuid7()}", data={"verdict": "animal"})).status_code == 404
    cid, _ = await _capture(app_client)
    # Unsure means skip in the client, not a stored third verdict.
    assert (await app_client.post(f"{BASE}/capture/{cid}", data={"verdict": "unsure"})).status_code == 422


async def test_missing_and_superseded_scores_remain_null_with_available_photos(app_client):
    await _moderator(app_client)
    cid, pid = await _capture(app_client, "failed")
    async with (await _pool(app_client)).acquire() as conn:
        await conn.execute("INSERT INTO detections(photo_id,model,dog,cat) VALUES($1,'old-model',0.8,0.1)", pid)
    response = (await app_client.get(f"{BASE}/capture/{cid}")).json()
    assert response["dog"] is None and response["cat"] is None
    assert response["thumb_url"]
    evidence = response["evidence"][0]
    assert evidence["dog"] is None and evidence["cat"] is None and evidence["model"] is None
    assert evidence["boxes"] == []
    assert str(cid) in await _ids(app_client, "no_detection")


async def test_source_boxes_and_crop_coordinates_are_not_interchangeable(app_client):
    await _moderator(app_client)
    cid, source = await _capture(app_client, "ready", scores=(0.95, 0.85), clip="clip.mp4")
    sid, sibling = uuid7(), uuid7()
    async with (await _pool(app_client)).acquire() as conn:
        for sighting in (sid, sibling):
            await conn.execute("INSERT INTO sightings(id,capture_id,captured_at) VALUES($1,$2,now())", sighting, cid)
        crop = await _instance(conn, cid, source, sid, score=0.2)
        sibling_crop = await _instance(conn, cid, source, sibling, species="cat", score=0.85)
    detail = (await app_client.get(f"{BASE}/sighting/{sid}")).json()
    assert detail["dog"] == pytest.approx(0.2)
    assert detail["cat"] == 0
    assert detail["clip_url"]
    by_id = {e["id"]: e for e in detail["evidence"]}
    assert set(by_id) == {str(source), str(crop)}
    assert str(sibling_crop) not in by_id
    assert by_id[str(crop)]["boxes"][0]["bbox"] == [10, 10, 110, 150]
    assert by_id[str(crop)]["width"] == 120
    assert {b["species"] for b in by_id[str(source)]["boxes"]} == {"dog"}
    assert by_id[str(source)]["evidence_kind"] == "source"
    assert by_id[str(crop)]["evidence_kind"] == "crop"
    assert by_id[str(source)]["boxes"][0]["bbox"] == [110, 70, 210, 210]
    assert by_id[str(source)]["model"] == "yolo26x"
    await app_client.post(f"/sighting/{sid}/animal", data={"verdict": "no_animal"})
    async with (await _pool(app_client)).acquire() as conn:
        assert await conn.fetchval("SELECT animal_override FROM sightings WHERE id=$1", sibling) is None
        assert await conn.fetchval("SELECT animal_override FROM captures WHERE id=$1", cid) is None
    assert (await app_client.post(f"{BASE}/capture/{cid}", data={"verdict": "animal"})).status_code == 409


async def test_legacy_no_boxes_is_not_no_detection_and_all_photos_are_visible(app_client):
    await _moderator(app_client)
    sid = await _sighting(app_client, animal_confidence=0.9,
        photos=[(0.9, 0, "first.webp"), (0.2, 0.3, "second.webp")])
    detail = (await app_client.get(f"{BASE}/sighting/{sid}")).json()
    assert len(detail["evidence"]) == 2
    assert all(e["boxes"] == [] for e in detail["evidence"])
    assert str(sid) not in await _ids(app_client, "no_detection")
    assert (await app_client.post(f"{BASE}/capture/{sid}", data={"verdict": "animal"})).status_code == 404


async def test_capture_verdict_is_reversible_and_changes_only_review_fields(app_client):
    mod = await _moderator(app_client)
    cid, _ = await _capture(app_client, "no_animal", scores=(0, 0))
    async with (await _pool(app_client)).acquire() as conn:
        before = dict(await conn.fetchrow("SELECT * FROM captures WHERE id=$1", cid))
    for verdict, override in (("animal", True), ("no_animal", False)):
        response = await app_client.post(f"{BASE}/capture/{cid}", data={"verdict": verdict})
        assert response.json() == {"status": "ok", "animal_override": override}
        async with (await _pool(app_client)).acquire() as conn:
            after = dict(await conn.fetchrow("SELECT * FROM captures WHERE id=$1", cid))
            assert await conn.fetchval("SELECT count(*) FROM sightings WHERE capture_id=$1", cid) == 0
            assert await conn.fetchval("SELECT count(*) FROM jobs WHERE capture_id=$1", cid) == 0
        assert after.pop("animal_override") is override
        assert after.pop("animal_reviewed_at") is not None
        assert after.pop("animal_reviewed_by") == mod
        assert after == {k: v for k, v in before.items() if k not in
                         ("animal_override", "animal_reviewed_at", "animal_reviewed_by")}
    assert str(cid) in await _ids(app_client, "reviewed")


async def test_prior_capture_verdict_does_not_propagate_to_future_publication(app_client):
    from app.capture_contracts import AnimalGroup
    from app.captures import publish_groups
    from app.matching import lock_matching
    await _moderator(app_client)
    cid, source = await _capture(app_client, "needs_review")
    await app_client.post(f"{BASE}/capture/{cid}", data={"verdict": "no_animal"})
    async with (await _pool(app_client)).acquire() as conn, conn.transaction():
        await _instance(conn, cid, source)
        await lock_matching(conn)
        capture = await conn.fetchrow("SELECT * FROM captures WHERE id=$1 FOR UPDATE", cid)
        iid = await conn.fetchval("SELECT id FROM animal_instances WHERE capture_id=$1", cid)
        sids = await publish_groups(conn, capture, [AnimalGroup(instance_ids=[iid])])
        assert await conn.fetchval("SELECT animal_override FROM sightings WHERE id=$1", sids[0]) is None
        assert await conn.fetchval("SELECT animal_override FROM captures WHERE id=$1", cid) is False
    assert str(cid) not in await _ids(app_client)
    assert str(sids[0]) in await _ids(app_client)


async def test_concurrent_publication_wins_and_review_returns_conflict(app_client):
    await _moderator(app_client)
    cid, _ = await _capture(app_client, "needs_review")
    pool = await _pool(app_client)
    async with pool.acquire() as publisher:
        async with publisher.transaction():
            await publisher.execute("SELECT id FROM captures WHERE id=$1 FOR UPDATE", cid)
            request = asyncio.create_task(app_client.post(f"{BASE}/capture/{cid}", data={"verdict": "no_animal"}))
            # Wait for the review query to reach the row lock, not an arbitrary
            # sleep that can let a race test pass without exercising the race.
            async with asyncio.timeout(5):
                while True:
                    async with pool.acquire() as probe:
                        waiting = await probe.fetchval("""SELECT EXISTS(SELECT 1 FROM pg_stat_activity
                            WHERE datname=current_database() AND wait_event_type='Lock'
                              AND query LIKE 'SELECT processing_state FROM captures%')""")
                    if waiting:
                        break
                    await asyncio.sleep(0.01)
            await publisher.execute("INSERT INTO sightings(id,capture_id,captured_at) VALUES($1,$2,now())", uuid7(), cid)
        response = await request
    assert response.status_code == 409
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT animal_override FROM captures WHERE id=$1", cid) is None


async def test_full_evidence_budget_and_presigning_are_batched(app_client, monkeypatch):
    from app.storage.s3 import S3Storage
    await _moderator(app_client)
    cid, _ = await _capture(app_client, "needs_review", photo=False)
    async with (await _pool(app_client)).acquire() as conn:
        for _ in range(48):
            source = uuid7()
            await conn.execute("INSERT INTO photos(id,capture_id,s3_key,width,height) VALUES($1,$2,$3,400,300)",
                source, cid, f"source/{source}.webp")
            await _instance(conn, cid, source)
            await _instance(conn, cid, source)
    signer = AsyncMock(side_effect=lambda keys: ["signed:" + key for key in keys])
    monkeypatch.setattr(S3Storage, "urls", signer)
    response = await app_client.get(f"{BASE}/capture/{cid}")
    assert response.status_code == 200, response.text
    assert len(response.json()["evidence"]) == 144
    assert signer.await_count == 1
    assert len(signer.call_args.args[0]) == 288
    signer.reset_mock()
    await app_client.get(BASE)
    assert signer.await_count == 1
    assert len(signer.call_args.args[0]) == 1


async def test_capture_review_columns_are_private_nullable_and_app_writable(migrated_db):
    for column in ("animal_override", "animal_reviewed_at", "animal_reviewed_by"):
        assert await migrated_db.fetchval("""SELECT is_nullable FROM information_schema.columns
            WHERE table_name='captures' AND column_name=$1""", column) == "YES"
        assert await migrated_db.fetchval(
            "SELECT has_column_privilege('app_rw','captures',$1,'SELECT,UPDATE')", column)
        assert not await migrated_db.fetchval(
            "SELECT has_column_privilege('public_read','captures',$1,'SELECT')", column)


async def test_failed_video_without_frames_has_clip_and_no_fabricated_image(app_client):
    await _moderator(app_client)
    cid, _ = await _capture(app_client, "failed", photo=False, clip="video.mp4")
    detail = (await app_client.get(f"{BASE}/capture/{cid}")).json()
    assert detail["evidence"] == []
    assert detail["clip_url"]
    assert detail["thumb_url"] is None
    assert detail["dog"] is None and detail["cat"] is None
