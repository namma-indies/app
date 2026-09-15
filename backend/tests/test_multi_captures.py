"""Capture contracts and publication regressions; DB cases use existing test fixtures."""
import io
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.capture_contracts import (AnimalGroup, CaptureReview, MultiCompletion,
    MultiURLRequest, MULTI_PIPELINE_VERSION, SourceFrame)
from app.capture_jobs import (claim_capture_job, complete_capture_job, fail_capture_job,
    multi_lease, upload_urls)
from app.captures import validate_groups
from app.config import settings
from app.media_jobs import Failure, claim_job
from tests.test_media_jobs import FakeConnection, lease_row


def result(needs_review=False, count=2):
    source = SourceFrame(photo_id=uuid4(),width=100,height=80,phash="0123456789abcdef",
        dog_confidence=0.9,cat_confidence=0.8)
    instances = [{"index":n,"source_photo_id":source.photo_id,"track_id":f"track-{n}",
        "species":"dog" if n == 0 else "cat","confidence":0.9,"bbox":(1,1,9,9),
        "crop_bbox":(0,0,10,10),"width":10,"height":10,"phash":"0123456789abcdef",
        "vector":[1.0 if i == n else 0.0 for i in range(2152)]} for n in range(count)]
    return MultiCompletion(lease_token="x"*43,pipeline_version=MULTI_PIPELINE_VERSION,
        model="miewid-msv3",frames=[source],instances=instances,needs_review=needs_review)


@pytest.mark.parametrize("change", [
    {"vector":[0.0]*2152},{"vector":[float("nan")]*2152},
    {"bbox":(0,0,200,20)},{"crop_bbox":(0,0,200,20)},
    {"species":"fox"},{"width":999},{"index":96},
])
def test_invalid_instance_rejected(change):
    body = result().model_dump()
    body["instances"][0].update(change)
    with pytest.raises(ValidationError):
        MultiCompletion.model_validate(body)


def test_no_animal_and_embedding_failure_are_distinct():
    assert not result(count=0).instances
    body = result().model_dump()
    body["instances"][0]["vector"] = None
    assert MultiCompletion.model_validate(body).instances[0].vector is None


def test_track_cannot_mix_covisible_animals_or_species():
    body = result().model_dump()
    body["instances"][1]["track_id"] = "track-0"
    with pytest.raises(ValidationError):
        MultiCompletion.model_validate(body)
    body["instances"][1]["species"] = "dog"
    with pytest.raises(ValidationError):
        MultiCompletion.model_validate(body)


def test_review_requires_exact_nonoverlapping_species_safe_partition():
    a,b,source = uuid4(),uuid4(),uuid4()
    rows = [{"id":a,"source_photo_id":source,"species":"dog"},
            {"id":b,"source_photo_id":source,"species":"dog"}]
    validate_groups(rows,[AnimalGroup(instance_ids=[a]),AnimalGroup(instance_ids=[b])])
    for groups in ([AnimalGroup(instance_ids=[a])],[AnimalGroup(instance_ids=[a,b])]):
        with pytest.raises(HTTPException):
            validate_groups(rows,groups)
    with pytest.raises(ValidationError):
        CaptureReview(revision=0,groups=[{"instance_ids":[a,a]}])
    rows[1]["source_photo_id"] = uuid4()
    rows[1]["species"] = "cat"
    with pytest.raises(HTTPException):
        validate_groups(rows,[AnimalGroup(instance_ids=[a,b])])


async def test_v1_and_v2_claims_are_disjoint():
    old,new = FakeConnection(),FakeConnection()
    await claim_job(old,"old")
    await claim_capture_job(new,"new")
    old_query = next((q,args) for q,args in old.calls if "FOR UPDATE SKIP LOCKED" in q)
    new_query = next((q,args) for q,args in new.calls if "FOR UPDATE SKIP LOCKED" in q)
    assert old_query[1][0] != new_query[1][0]
    assert "capture_id IS NOT NULL" in new_query[0]
    assert "sighting_id IS NOT NULL" in old_query[0]


async def test_v2_lease_rejects_legacy_job():
    conn = FakeConnection(lease_row(capture_id=None))
    with pytest.raises(HTTPException):
        await multi_lease(conn,conn.job["id"],"x"*43)


async def test_v2_slots_are_bounded_and_lease_scoped():
    conn = FakeConnection(lease_row(capture_id=uuid4(),pipeline_version=MULTI_PIPELINE_VERSION))
    storage = SimpleNamespace(put_url=AsyncMock(return_value="put"),urls=AsyncMock(return_value=[]))
    body = MultiURLRequest(lease_token="x"*43,uploads=[{"kind":"evidence","index":0,
        "original_bytes":50,"thumbnail_bytes":10}])
    response = await upload_urls(conn,storage,conn.job["id"],body)
    assert response["uploads"][0]["kind"] == "evidence"
    assert conn.job["lease_token_hash"] in storage.put_url.call_args_list[0].args[0]
    with pytest.raises(ValidationError):
        MultiURLRequest(lease_token="x"*43,uploads=[{"kind":"frame","index":48,
            "original_bytes":50,"thumbnail_bytes":10}])


async def test_cpu_fallback_uses_multi_adapter_and_fenced_completion(monkeypatch):
    import numpy as np
    from app import analyse, capture_cpu
    from app.detect_reid import AnimalDetection
    from app.tracking import SamplingCoverage, associate_frames, make_frame
    from tests.test_media_jobs import FakePool
    source_id = uuid4()
    detection = AnimalDetection(detection_index=0,species="dog",confidence=0.9,
        raw_box=(1,1,9,9),crop_box=(0,0,10,10))
    frame = make_frame("photo:0",0,None,100,80,0.9,0,[(detection,np.array([1.0]+[0.0]*2151),None)])
    analysis = associate_frames((frame,),SamplingCoverage("photos",1,1,None,None,None,12,12,True))
    monkeypatch.setattr(analyse,"analyse_photos",lambda raw:analysis)
    monkeypatch.setattr(capture_cpu,"evidence_photo",lambda raw,box:SimpleNamespace(
        original=b"crop",thumbnail=b"thumb",content_type="image/webp",width=10,height=10,phash="0123456789abcdef"))
    monkeypatch.setattr(capture_cpu,"capture_sources",AsyncMock(return_value=[{
        "photo_id":source_id,"s3_key":"source","width":100,"height":80,"phash":"0123456789abcdef"}]))
    monkeypatch.setattr(capture_cpu,"upload_urls",AsyncMock())
    monkeypatch.setattr(capture_cpu,"multi_lease",AsyncMock(return_value={"payload":{"slots":{"evidence:0":{"key":"staged"}}}}))
    finish = AsyncMock()
    monkeypatch.setattr(capture_cpu,"complete_capture_job",finish)
    storage = SimpleNamespace(get=AsyncMock(return_value=b"source"),put=AsyncMock())
    await capture_cpu.cpu_process_capture(FakePool(FakeConnection()),storage,{"kind":"photo","id":uuid4()},"x"*43)
    completion = finish.await_args.args[3]
    assert finish.await_args.kwargs["cpu"] is True
    assert completion.pipeline_version == MULTI_PIPELINE_VERSION
    assert completion.instances[0].source_photo_id == source_id
    assert not completion.needs_review
    assert storage.put.await_count == 2


async def seed(conn,body,kind="photo"):
    oid,cid,jid = uuid4(),uuid4(),uuid4()
    await conn.execute("INSERT INTO observers(id) VALUES($1)",oid)
    await conn.execute("INSERT INTO captures(id,observer_id,kind,captured_at,attrs,processing_state) VALUES($1,$2,$3,now(),'{\"note\":\"shared\"}','processing')",cid,oid,kind)
    if kind == "photo":
        for f in body.frames:
            await conn.execute("INSERT INTO photos(id,capture_id,s3_key,width,height,phash) VALUES($1,$2,$3,$4,$5,$6)",
                f.photo_id,cid,f"source/{f.photo_id}.webp",f.width,f.height,f.phash)
    from app.media_jobs import token_hash
    await conn.execute("""INSERT INTO jobs(id,capture_id,kind,pipeline_version,status,lease_owner,
        lease_token_hash,lease_expires_at,attempts) VALUES($1,$2,$3,$4,'running','gpu:test',$5,now()+interval '5 minutes',1)""",
        jid,cid,kind,MULTI_PIPELINE_VERSION,token_hash(body.lease_token))
    storage = SimpleNamespace(put_url=AsyncMock(return_value="put"),
        urls=AsyncMock(side_effect=lambda keys,**kw:["signed:"+k for k in keys]),publish_checked=AsyncMock())
    if body.instances:
        await upload_urls(conn,storage,jid,MultiURLRequest(lease_token=body.lease_token,uploads=[{
            "kind":"evidence","index":i.index,"original_bytes":50,"thumbnail_bytes":10} for i in body.instances]))
    return oid,cid,jid,storage


async def test_db_completion_isolated_children_and_replay(migrated_db):
    conn = migrated_db
    body = result()
    oid,cid,jid,storage = await seed(conn,body)
    response = await complete_capture_job(conn,storage,jid,body)
    assert response["outcome"] == "ready"
    children = await conn.fetch("SELECT id,species,vec_miew::text AS vec FROM sightings WHERE capture_id=$1 ORDER BY species",cid)
    assert len(children) == 2
    assert {r["species"] for r in children} == {"dog","cat"}
    assert children[0]["vec"] != children[1]["vec"]
    assert await conn.fetchval("SELECT count(*) FROM photos WHERE capture_id=$1 AND sighting_id IS NULL",cid) == 1
    assert await conn.fetchval("SELECT count(*) FROM embeddings e JOIN animal_instances a ON a.id=e.instance_id WHERE a.capture_id=$1",cid) == 2
    assert await conn.fetchval("SELECT count(*) FROM match_proposals WHERE sighting_id=ANY($1::uuid[])",[r["id"] for r in children]) == 0
    storage.publish_checked.reset_mock()
    assert (await complete_capture_job(conn,storage,jid,body))["replayed"]
    storage.publish_checked.assert_not_awaited()
    changed = body.model_copy(update={"needs_review":True})
    with pytest.raises(HTTPException):
        await complete_capture_job(conn,storage,jid,changed)


async def test_db_private_review_owner_revision_and_publication(migrated_db):
    from app.routes.capture import review_capture, get_capture
    from app.capture_contracts import CaptureReview
    conn = migrated_db
    body = result(needs_review=True)
    oid,cid,jid,storage = await seed(conn,body)
    await complete_capture_job(conn,storage,jid,body)
    assert await conn.fetchval("SELECT count(*) FROM sightings WHERE capture_id=$1",cid) == 0
    status = await get_capture(cid,oid,conn,storage)
    assert status.processing_state == "needs_review" and len(status.instances) == 2
    review = CaptureReview(revision=status.revision,groups=status.groups,publish=False)
    with pytest.raises(HTTPException) as exc:
        await review_capture(cid,review,uuid4(),conn,storage)
    assert exc.value.status_code == 404
    draft = await review_capture(cid,review,oid,conn,storage)
    assert not draft.sighting_ids
    with pytest.raises(HTTPException):
        await review_capture(cid,review,oid,conn,storage)
    published = await review_capture(cid,CaptureReview(revision=draft.revision,groups=draft.groups),oid,conn,storage)
    assert len(published.sighting_ids) == 2
    assert published.processing_state == "ready"
    updated_groups = [group.model_copy(update={"condition": "injured" if index == 0 else "healthy"})
                      for index, group in enumerate(published.groups)]
    updated = await review_capture(cid,CaptureReview(revision=published.revision,groups=updated_groups),oid,conn,storage)
    assert updated.sighting_ids == published.sighting_ids
    for group in updated.groups:
        condition = await conn.fetchval("""SELECT s.attrs->>'condition' FROM sightings s
            JOIN animal_instances a ON a.sighting_id=s.id WHERE a.id=$1""",group.instance_ids[0])
        assert condition == group.condition
    with pytest.raises(HTTPException) as exc:
        await review_capture(cid,CaptureReview(revision=updated.revision,groups=updated.groups,publish=False),oid,conn,storage)
    assert exc.value.status_code == 409


async def test_db_noanimal_failure_and_stale_completion(migrated_db):
    conn = migrated_db
    body = result(count=0)
    oid,cid,jid,storage = await seed(conn,body)
    assert (await complete_capture_job(conn,storage,jid,body))["outcome"] == "no_animal"
    assert await conn.fetchval("SELECT count(*) FROM sightings WHERE capture_id=$1",cid) == 0
    other = result()
    _,cid2,jid2,storage2 = await seed(conn,other)
    await fail_capture_job(conn,jid2,Failure(lease_token=other.lease_token,error="decode failed",retryable=False))
    assert await conn.fetchval("SELECT processing_state FROM captures WHERE id=$1",cid2) == "failed"
    with pytest.raises(HTTPException):
        await complete_capture_job(conn,storage2,jid2,other)


@pytest.mark.parametrize("second_species", ["dog", "cat"])
async def test_db_shared_reports_and_species_matching(migrated_db, second_species):
    from app.routes.moderation import report_sighting, review_sighting
    from app.matching import ensure_compatible_observations, find_candidates
    import numpy as np
    conn = migrated_db
    body = result()
    body.instances[1].species = second_species
    oid,cid,jid,storage = await seed(conn,body)
    await complete_capture_job(conn,storage,jid,body)
    ids = [r["id"] for r in await conn.fetch("SELECT id FROM sightings WHERE capture_id=$1 ORDER BY id",cid)]
    with pytest.raises(HTTPException):
        await ensure_compatible_observations(conn,ids)
    assert not await find_candidates(conn,np.asarray(body.instances[0].vector),lat=None,lng=None,
        radius_m=1000,exclude_sighting_id=ids[0])
    second = uuid4()
    await conn.execute("INSERT INTO observers(id) VALUES($1)",second)
    assert not (await report_sighting(ids[0],"offensive",None,oid,conn))["hidden"]
    assert (await report_sighting(ids[1],"offensive",None,second,conn))["hidden"]
    assert await conn.fetchval("SELECT count(*) FROM sightings WHERE capture_id=$1 AND review_status='pending'",cid) == 2
    await review_sighting(ids[0],"rejected",oid,conn)
    assert await conn.fetchval("SELECT count(*) FROM sightings WHERE capture_id=$1 AND review_status='rejected'",cid) == 2


async def test_db_upload_gate_and_cross_endpoint_idempotency(migrated_db,monkeypatch):
    from fastapi import UploadFile
    from app.routes.capture import create_capture
    from tests.test_media_jobs import FakePool
    conn = migrated_db
    oid = uuid4()
    await conn.execute("INSERT INTO observers(id) VALUES($1)",oid)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=FakePool(conn))))
    storage = SimpleNamespace(put=AsyncMock())
    kwargs = dict(request=request,photos=None,video=UploadFile(io.BytesIO(b"video")),lat=None,lng=None,
        geo_accuracy_m=None,client_token="capture-token",geo_source="none",captured_at=datetime.now(timezone.utc),
        reported_at=None,note="shared",observer_id=oid,storage=storage)
    monkeypatch.setattr(settings,"multi_animal_enabled",False)
    with pytest.raises(HTTPException) as exc:
        await create_capture(**kwargs)
    assert exc.value.status_code == 404
    monkeypatch.setattr(settings,"multi_animal_enabled",True)
    monkeypatch.setattr(settings,"media_jobs_enabled",True)
    received = await create_capture(**kwargs)
    assert received["processing_state"] == "queued" and not received["sighting_ids"]
    monkeypatch.setattr(settings,"multi_animal_enabled",False)
    assert (await create_capture(**kwargs))["duplicate"]
    legacy = uuid4()
    await conn.execute("INSERT INTO sightings(id,observer_id,client_token,captured_at) VALUES($1,$2,'legacy-token',now())",legacy,oid)
    kwargs["client_token"] = "legacy-token"
    replay = await create_capture(**kwargs)
    assert replay["capture_id"] == legacy and replay["sighting_ids"] == [legacy]
