"""Versioned v2 jobs reuse v1 durable leases, never its largest-animal completion.

/internal/capture-jobs: claim {owner}, /{id}/heartbeat {lease_token},
/{id}/urls MultiURLRequest, /{id}/complete MultiCompletion, /{id}/fail Failure.
URL slots return kind,index,photo_id,original_url,thumbnail_url,content_type.
Evidence index is AnimalResult.index; video frames use SourceFrame.index.
"""
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone
from uuid import UUID, uuid5

from fastapi import APIRouter, Depends, HTTPException, Request

from app.capture_contracts import (AnimalGroup, MAX_INSTANCES, MAX_SOURCE_FRAMES,
    MULTI_PIPELINE_VERSION, MultiCompletion, MultiURLRequest)
from app.captures import publish_groups
from app.config import settings
from app.deps import get_storage
from app.ids import uuid7
from app.media_jobs import (Claim, EMBED_DIM, Failure, Lease, MODEL_NAME, heartbeat_job,
    locked_lease, payload_of, require_gpu, token_hash, vector_text)
from app.photos import thumb_key
from app.scoring import save_detection

router = APIRouter(prefix="/internal/capture-jobs",dependencies=[Depends(require_gpu)])


async def claim_capture_job(conn, owner, *, cpu=False):
    token = secrets.token_urlsafe(32)
    async with conn.transaction():
        if cpu:
            await conn.execute("SELECT pg_advisory_xact_lock(734821901)")
        else:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", "media-worker:" + owner)
        lease_owner = "cpu" if cpu else "gpu:" + owner
        if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM jobs WHERE lease_owner=$1 AND status='running' AND lease_expires_at>clock_timestamp())",lease_owner):
            return None
        job = await conn.fetchrow("""SELECT * FROM jobs WHERE capture_id IS NOT NULL AND pipeline_version=$1
            AND ((status='pending' AND COALESCE(run_after,now())<=now())
                OR (status='running' AND lease_expires_at<=now()))
            AND (NOT $2 OR (kind='photo' AND cpu_eligible_at<=now()))
            ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1""",MULTI_PIPELINE_VERSION,cpu)
        if job is None:
            return None
        if job["attempts"] >= settings.media_max_attempts:
            await conn.execute("UPDATE jobs SET status='failed',terminal_outcome='failed',lease_token_hash=NULL,lease_expires_at=NULL,last_error='lease retry limit reached',updated_at=now() WHERE id=$1",job["id"])
            await conn.execute("UPDATE captures SET processing_state='failed',updated_at=now() WHERE id=$1",job["capture_id"])
            return None
        job = await conn.fetchrow("""UPDATE jobs SET status='running',attempts=attempts+1,lease_token_hash=$2,
            lease_owner=$3,lease_expires_at=now()+$4*interval '1 second',payload='{}',updated_at=now()
            WHERE id=$1 RETURNING *""",job["id"],token_hash(token),lease_owner,settings.media_lease_s)
        await conn.execute("UPDATE captures SET processing_state='processing',updated_at=now() WHERE id=$1",job["capture_id"])
    return job,token


async def capture_sources(conn,job):
    if job["kind"] == "video":
        key = await conn.fetchval("SELECT clip_s3_key FROM captures WHERE id=$1",job["capture_id"])
        if not key:
            raise HTTPException(409,"missing video source")
        return [{"photo_id":None,"s3_key":key}]
    return await conn.fetch("""SELECT p.id AS photo_id,p.s3_key,p.width,p.height,p.phash FROM photos p
        WHERE p.capture_id=$1 AND NOT EXISTS(SELECT 1 FROM animal_instances a WHERE a.evidence_photo_id=p.id)
        ORDER BY p.created_at,p.id""",job["capture_id"])


def ttl(job):
    return max(1,min(settings.media_url_s,int((job["lease_expires_at"]-datetime.now(timezone.utc)).total_seconds())))


async def signed_sources(conn,job,storage):
    rows = await capture_sources(conn,job)
    urls = await storage.urls([r["s3_key"] for r in rows],expires_s=ttl(job))
    return [{"photo_id":str(r["photo_id"]) if r["photo_id"] else None,"url":url,
        **({k:r[k] for k in ("width","height","phash")} if job["kind"] == "photo" else {})}
        for r,url in zip(rows,urls)]


async def multi_lease(conn,job_id,token,**kwargs):
    job = await locked_lease(conn,job_id,token,**kwargs)
    if job["pipeline_version"] != MULTI_PIPELINE_VERSION or job["capture_id"] is None:
        raise HTTPException(409,"not a multi-animal job")
    return job


@router.post("/claim")
async def claim(body: Claim,request: Request,storage=Depends(get_storage)):
    async with request.app.state.pool.acquire() as conn:
        result = await claim_capture_job(conn,body.owner)
        if result is None:
            return {"job":None}
        job,token = result
        async with conn.transaction():
            job = await multi_lease(conn,job["id"],token)
            sources = await signed_sources(conn,job,storage)
        return {"job":{"id":str(job["id"]),"capture_id":str(job["capture_id"]),"kind":job["kind"],
            "pipeline_version":MULTI_PIPELINE_VERSION,"model":MODEL_NAME,"lease_token":token,
            "lease_expires_at":job["lease_expires_at"],"sources":sources,
            "max_frames":MAX_SOURCE_FRAMES,"max_instances":MAX_INSTANCES}}


@router.post("/{job_id}/heartbeat")
async def heartbeat(job_id: UUID,body: Lease,request: Request):
    async with request.app.state.pool.acquire() as conn,conn.transaction():
        await multi_lease(conn,job_id,body.lease_token)
        return {"lease_expires_at":await heartbeat_job(conn,job_id,body.lease_token)}


async def upload_urls(conn,storage,job_id,body: MultiURLRequest,*,cpu=False):
    async with conn.transaction():
        job = await multi_lease(conn,job_id,body.lease_token,cpu=cpu)
        slots = payload_of(job).get("slots",{})
        for item in body.uploads:
            if item.kind == "frame" and job["kind"] != "video":
                raise HTTPException(422,"photo jobs cannot upload source frames")
            name = f"{item.kind}:{item.index}"
            spec = item.model_dump()
            if name in slots and slots[name]["spec"] != spec:
                raise HTTPException(409,"slot specification cannot change")
            pid = uuid5(job_id,job["lease_token_hash"] + ":" + name)
            key = f"media-staging/{job_id}/{job['lease_token_hash']}/{pid}.webp"
            slots[name] = {"photo_id":str(pid),"key":key,"spec":spec}
        if sum(slot["spec"]["original_bytes"] + slot["spec"]["thumbnail_bytes"] for slot in slots.values()) > 256 * 1024 * 1024:
            raise HTTPException(422,"capture evidence exceeds aggregate byte budget")
        await conn.execute("UPDATE jobs SET payload=$2::jsonb WHERE id=$1",job_id,json.dumps({"slots":slots}))
        uploads = []
        for item in body.uploads:
            slot = slots[f"{item.kind}:{item.index}"]
            uploads.append({"kind":item.kind,"index":item.index,"photo_id":slot["photo_id"],
                "original_url":await storage.put_url(slot["key"],item.original_bytes,ttl(job)),
                "thumbnail_url":await storage.put_url(thumb_key(slot["key"]),item.thumbnail_bytes,ttl(job)),
                "content_type":"image/webp"})
        return {"sources":await signed_sources(conn,job,storage),"uploads":uploads}


@router.post("/{job_id}/urls")
async def urls(job_id: UUID,body: MultiURLRequest,request: Request,storage=Depends(get_storage)):
    async with request.app.state.pool.acquire() as conn:
        return await upload_urls(conn,storage,job_id,body)


async def publish_slot(conn,storage,job,slot,width,height,phash):
    pid = UUID(slot["photo_id"])
    key = f"captures/{job['capture_id']}/evidence/{pid}.webp"
    for src,dst,size in ((slot["key"],key,slot["spec"]["original_bytes"]),
            (thumb_key(slot["key"]),thumb_key(key),slot["spec"]["thumbnail_bytes"])):
        await storage.publish_checked(src,dst,size)
    await conn.execute("INSERT INTO photos(id,capture_id,s3_key,width,height,phash) VALUES($1,$2,$3,$4,$5,$6)",
        pid,job["capture_id"],key,width,height,phash)
    return pid


async def complete_capture_job(conn,storage,job_id,body: MultiCompletion,*,cpu=False):
    content = body.model_dump(mode="json",exclude={"lease_token"})
    content["frames"].sort(key=lambda f:str(f["photo_id"]))
    content["instances"].sort(key=lambda i:i["index"])
    digest = hashlib.sha256(json.dumps(content,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()
    from app.matching import lock_matching
    async with conn.transaction():
        job = await multi_lease(conn,job_id,body.lease_token,cpu=cpu,allow_done=True)
        if job["status"] == "done":
            if not hmac.compare_digest(job["completion_digest"],digest):
                raise HTTPException(409,"completion differs from committed result")
            return {"status":"done","outcome":job["terminal_outcome"],"replayed":True}
        if cpu and job["kind"] != "photo":
            raise HTTPException(409,"CPU cannot process video")
        await lock_matching(conn)
        capture = await conn.fetchrow("SELECT * FROM captures WHERE id=$1 FOR UPDATE",job["capture_id"])
        slots = payload_of(job).get("slots",{})
        if job["kind"] == "photo":
            sources = {r["photo_id"]:r for r in await capture_sources(conn,job)}
            if set(sources) != {f.photo_id for f in body.frames}:
                raise HTTPException(422,"completion must cover exactly the source photos")
            for frame in body.frames:
                if frame.index is not None or frame.timestamp_ms is not None or any(
                        getattr(frame,k) != sources[frame.photo_id][k] for k in ("width","height","phash")):
                    raise HTTPException(422,"source metadata mismatch")
        else:
            if len({f.index for f in body.frames}) != len(body.frames):
                raise HTTPException(422,"duplicate video frame indices")
            timestamps = [f.timestamp_ms for f in body.frames]
            if any(t is None for t in timestamps) or len(set(timestamps)) != len(timestamps):
                raise HTTPException(422,"video source timestamps must be distinct")
            for frame in body.frames:
                slot = slots.get(f"frame:{frame.index}")
                if frame.timestamp_ms is None or slot is None or slot["photo_id"] != str(frame.photo_id):
                    raise HTTPException(422,"unknown or untimed video frame")
                await publish_slot(conn,storage,job,slot,frame.width,frame.height,frame.phash)
        frames = {f.photo_id:f for f in body.frames}
        for frame in body.frames:
            await save_detection(conn,frame.photo_id,frame.dog_confidence,frame.cat_confidence)
        groups = {}
        for item in body.instances:
            slot = slots.get(f"evidence:{item.index}")
            if slot is None:
                raise HTTPException(422,"missing animal evidence upload")
            pid = await publish_slot(conn,storage,job,slot,item.width,item.height,item.phash)
            iid = uuid5(capture["id"],f"instance:{item.index}")
            await conn.execute("""INSERT INTO animal_instances(id,capture_id,source_photo_id,evidence_photo_id,
                track_id,species,confidence,bbox,crop_bbox,timestamp_ms)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10)""",iid,capture["id"],
                item.source_photo_id,pid,item.track_id,item.species,item.confidence,json.dumps(item.bbox),
                json.dumps(item.crop_bbox),frames[item.source_photo_id].timestamp_ms)
            await save_detection(conn,pid,item.confidence if item.species == "dog" else 0,
                item.confidence if item.species == "cat" else 0)
            if item.vector is not None:
                await conn.execute("""INSERT INTO embeddings(id,photo_id,instance_id,model,dim,vec_miew,bbox)
                    VALUES($1,$2,$3,$4,$5,$6::vector,$7::jsonb)""",uuid7(),pid,iid,MODEL_NAME,EMBED_DIM,
                    vector_text(item.vector),json.dumps(dict(zip(("x1","y1","x2","y2"),item.crop_bbox))))
            groups.setdefault(item.track_id,[]).append(iid)
        outcome = "no_animal" if not body.instances else "needs_review" if body.needs_review else "ready"
        if outcome == "ready":
            await publish_groups(conn,capture,[AnimalGroup(instance_ids=ids) for ids in groups.values()])
        await conn.execute("UPDATE captures SET processing_state=$2,revision=revision+1,updated_at=now() WHERE id=$1",capture["id"],outcome)
        if not await conn.fetchval("SELECT lease_expires_at>clock_timestamp() FROM jobs WHERE id=$1",job_id):
            raise HTTPException(409,"lease expired during completion")
        await conn.execute("UPDATE jobs SET status='done',completion_digest=$2,terminal_outcome=$3,updated_at=now() WHERE id=$1",job_id,digest,outcome)
        return {"status":"done","outcome":outcome,"replayed":False}


@router.post("/{job_id}/complete")
async def complete(job_id: UUID,body: MultiCompletion,request: Request,storage=Depends(get_storage)):
    async with request.app.state.pool.acquire() as conn:
        return await complete_capture_job(conn,storage,job_id,body)


async def fail_capture_job(conn,job_id,body: Failure,*,cpu=False):
    async with conn.transaction():
        job = await multi_lease(conn,job_id,body.lease_token,cpu=cpu)
        retry = body.retryable and job["attempts"] < settings.media_max_attempts
        await conn.execute("""UPDATE jobs SET status=$2,last_error=$3,run_after=now()+$4*interval '1 second',
            lease_token_hash=NULL,lease_expires_at=NULL,terminal_outcome=$5,updated_at=now() WHERE id=$1""",
            job_id,"pending" if retry else "failed",body.error,min(300,2**min(job["attempts"],8)),None if retry else "failed")
        await conn.execute("UPDATE captures SET processing_state=$2,updated_at=now() WHERE id=$1",job["capture_id"],"queued" if retry else "failed")
        return {"status":"queued" if retry else "failed"}


@router.post("/{job_id}/fail")
async def fail(job_id: UUID,body: Failure,request: Request):
    async with request.app.state.pool.acquire() as conn:
        return await fail_capture_job(conn,job_id,body)
