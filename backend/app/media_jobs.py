"""Durable stored-WebP pipeline and shared GPU worker wire schema.

GPU API prefix: /internal/media-jobs. POST claim {owner}; POST /{id}/
heartbeat {lease_token}; POST /{id}/urls {lease_token, frames:[{index,
original_bytes, thumbnail_bytes}]}; POST /{id}/complete Completion; POST
/{id}/fail {lease_token, error, retryable}. Every call requires the configured
GPU bearer token. CPU capability is internal only, never a request parameter.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import math
import secrets
from contextlib import suppress
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid5

import numpy as np
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.deps import get_storage
from app.ids import uuid7
from app.photos import thumb_key

PIPELINE_VERSION = "stored-webp-q90-yolo26x-miewid-msv3-v1"
MODEL_NAME = "miewid-msv3"
EMBED_DIM = 2152
MAX_FRAMES = 12
MAX_PHOTOS = 24
MAX_FRAME_BYTES = 20 * 1024 * 1024
MAX_THUMB_BYTES = 1024 * 1024
logger = logging.getLogger(__name__)


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Claim(WireModel):
    owner: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.:-]+$")


class Lease(WireModel):
    lease_token: str = Field(min_length=32, max_length=128)


class FrameUpload(WireModel):
    index: int = Field(ge=0, lt=MAX_FRAMES)
    original_bytes: int = Field(gt=0, le=MAX_FRAME_BYTES)
    thumbnail_bytes: int = Field(gt=0, le=MAX_THUMB_BYTES)


class URLRequest(Lease):
    frames: list[FrameUpload] = Field(default_factory=list, max_length=MAX_FRAMES)


class FrameResult(WireModel):
    photo_id: UUID
    index: int | None = Field(default=None, ge=0, lt=MAX_FRAMES)
    width: int = Field(gt=0, le=16384)
    height: int = Field(gt=0, le=16384)
    phash: str = Field(pattern=r"^[0-9a-f]{16}$")
    dog_confidence: float = Field(ge=0, le=1)
    cat_confidence: float = Field(ge=0, le=1)
    bbox: tuple[int, int, int, int] | None = None
    vector: list[float] | None = Field(default=None, min_length=EMBED_DIM, max_length=EMBED_DIM)

    @model_validator(mode="after")
    def validate_analysis(self):
        if (self.bbox is None) != (self.vector is None):
            raise ValueError("bbox and vector must both be present or absent")
        if self.bbox is not None:
            x1, y1, x2, y2 = self.bbox
            if not (0 <= x1 < x2 <= self.width and 0 <= y1 < y2 <= self.height):
                raise ValueError("bbox outside frame")
        if self.vector is not None:
            if not all(math.isfinite(v) for v in self.vector):
                raise ValueError("vector must be finite")
            norm = math.sqrt(sum(v * v for v in self.vector))
            if abs(norm - 1) > 0.01:
                raise ValueError("vector must be L2-normalized")
        return self


class Completion(Lease):
    pipeline_version: Literal[PIPELINE_VERSION]
    model: Literal[MODEL_NAME]
    frames: list[FrameResult] = Field(min_length=1, max_length=MAX_PHOTOS)

    @model_validator(mode="after")
    def unique_frames(self):
        if len({f.photo_id for f in self.frames}) != len(self.frames):
            raise ValueError("duplicate photo IDs")
        return self


class Failure(Lease):
    error: str = Field(min_length=1, max_length=500)
    retryable: bool = True


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def completion_digest(body: Completion) -> str:
    content = body.model_dump(mode="json", exclude={"lease_token"})
    content["frames"].sort(key=lambda frame: str(frame["photo_id"]))
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def payload_of(job):
    value = job["payload"]
    return json.loads(value) if isinstance(value, str) else value


async def require_gpu(authorization: str | None = Header(None)):
    expected = settings.media_gpu_token
    supplied = (authorization or "").removeprefix("Bearer ")
    if (not settings.media_jobs_enabled or not expected or
            not authorization or not authorization.startswith("Bearer ") or
            not hmac.compare_digest(supplied.encode(), expected.encode())):
        raise HTTPException(401, "worker authentication required")


router = APIRouter(prefix="/internal/media-jobs", dependencies=[Depends(require_gpu)])


async def enqueue(conn, sighting_id: UUID, kind: str):
    if kind not in ("photo", "video"):
        raise ValueError("invalid media kind")
    await conn.execute(
        """INSERT INTO jobs (id, kind, sighting_id, pipeline_version, cpu_eligible_at, run_after)
           VALUES ($1,$2,$3,$4,now()+$5*interval '1 second',now())
           ON CONFLICT (sighting_id,pipeline_version) WHERE sighting_id IS NOT NULL DO NOTHING""",
        uuid7(), kind, sighting_id, PIPELINE_VERSION, settings.media_cpu_grace_s,
    )
    await conn.execute("UPDATE sightings SET processing_state='queued' WHERE id=$1", sighting_id)


async def claim_job(conn, owner: str, *, cpu: bool = False):
    token = secrets.token_urlsafe(32)
    async with conn.transaction():
        if not cpu:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", "media-worker:" + owner)
            if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM jobs WHERE lease_owner=$1 AND status='running' AND lease_expires_at>clock_timestamp())", "gpu:" + owner):
                return None
        # One fallback globally, including multi-process API deployments.
        if cpu:
            await conn.execute("SELECT pg_advisory_xact_lock(734821901)")
            if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM jobs WHERE lease_owner='cpu' AND status='running' AND lease_expires_at>now())"):
                return None
        job = await conn.fetchrow(
            """SELECT * FROM jobs WHERE sighting_id IS NOT NULL AND pipeline_version=$1
               AND ((status='pending' AND COALESCE(run_after,now())<=now())
                    OR (status='running' AND lease_expires_at<=now()))
               AND (NOT $2 OR (kind='photo' AND cpu_eligible_at<=now()))
               ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1""", PIPELINE_VERSION, cpu,
        )
        if job is None:
            return None
        if job["attempts"] >= settings.media_max_attempts:
            await conn.execute("UPDATE jobs SET status='failed',terminal_outcome='failed',lease_token_hash=NULL,lease_expires_at=NULL,last_error='lease retry limit reached',updated_at=now() WHERE id=$1", job["id"])
            await conn.execute("UPDATE sightings SET processing_state='failed' WHERE id=$1", job["sighting_id"])
            return None
        job = await conn.fetchrow(
            """UPDATE jobs SET status='running',attempts=attempts+1, lease_token_hash=$2,
               lease_owner=$3,lease_expires_at=now()+$4*interval '1 second',payload='{}',updated_at=now()
               WHERE id=$1 RETURNING *""", job["id"], token_hash(token),
            "cpu" if cpu else "gpu:" + owner, settings.media_lease_s,
        )
        await conn.execute("UPDATE sightings SET processing_state='processing' WHERE id=$1", job["sighting_id"])
    return job, token


async def locked_lease(conn, job_id, token, *, cpu=False, allow_done=False):
    job = await conn.fetchrow("SELECT *, lease_expires_at>clock_timestamp() AS live FROM jobs WHERE id=$1 FOR UPDATE", job_id)
    valid_token = job and job["lease_token_hash"] and hmac.compare_digest(job["lease_token_hash"], token_hash(token))
    valid_owner = job and ((job["lease_owner"] == "cpu") if cpu else (job["lease_owner"] or "").startswith("gpu:"))
    if not valid_token or not valid_owner or (not (allow_done and job["status"] == "done") and (job["status"] != "running" or not job["live"])):
        raise HTTPException(409, "stale or invalid lease")
    return job


async def source_rows(conn, job):
    if job["kind"] == "video":
        key = await conn.fetchval("SELECT clip_s3_key FROM sightings WHERE id=$1", job["sighting_id"])
        if not key:
            raise HTTPException(409, "video source is missing")
        return [{"photo_id": None, "s3_key": key}]
    return await conn.fetch("SELECT id AS photo_id,s3_key,width,height,phash FROM photos WHERE sighting_id=$1 ORDER BY created_at,id", job["sighting_id"])


async def signed_sources(conn, job, storage):
    rows = await source_rows(conn, job)
    ttl = max(1, min(settings.media_url_s, int((job["lease_expires_at"] - datetime.now(timezone.utc)).total_seconds())))
    urls = await storage.urls([r["s3_key"] for r in rows], expires_s=ttl)
    return [{"photo_id": str(r["photo_id"]) if r["photo_id"] else None, "url": url,
             **({k: r[k] for k in ("width", "height", "phash")} if job["kind"] == "photo" else {})}
            for r, url in zip(rows, urls)]


@router.post("/claim")
async def claim(body: Claim, request: Request, storage=Depends(get_storage)):
    async with request.app.state.pool.acquire() as conn:
        result = await claim_job(conn, body.owner)
        if result is None:
            return {"job": None}
        job, token = result
        async with conn.transaction():
            job = await locked_lease(conn, job["id"], token)
            sources = await signed_sources(conn, job, storage)
        return {"job": {"id": str(job["id"]), "sighting_id": str(job["sighting_id"]),
                        "kind": job["kind"], "pipeline_version": job["pipeline_version"],
                        "model": MODEL_NAME, "lease_token": token,
                        "lease_expires_at": job["lease_expires_at"], "sources": sources,
                        "max_frames": MAX_FRAMES}}


async def heartbeat_job(conn, job_id, token, *, cpu=False):
    async with conn.transaction():
        await locked_lease(conn, job_id, token, cpu=cpu)
        return await conn.fetchval("UPDATE jobs SET lease_expires_at=now()+$2*interval '1 second',updated_at=now() WHERE id=$1 RETURNING lease_expires_at", job_id, settings.media_lease_s)


@router.post("/{job_id}/heartbeat")
async def heartbeat(job_id: UUID, body: Lease, request: Request):
    async with request.app.state.pool.acquire() as conn:
        return {"lease_expires_at": await heartbeat_job(conn, job_id, body.lease_token)}


@router.post("/{job_id}/urls")
async def refresh_urls(job_id: UUID, body: URLRequest, request: Request, storage=Depends(get_storage)):
    async with request.app.state.pool.acquire() as conn, conn.transaction():
        job = await locked_lease(conn, job_id, body.lease_token)
        if body.frames and job["kind"] != "video":
            raise HTTPException(422, "photo jobs cannot upload frames")
        slots = payload_of(job).get("slots", {})
        for frame in body.frames:
            index = str(frame.index)
            spec = frame.model_dump()
            if index in slots and slots[index]["spec"] != spec:
                raise HTTPException(409, "frame slot sizes cannot change")
            photo_id = uuid5(job_id, job["lease_token_hash"] + ":" + index)
            key = f"media-staging/{job_id}/{job['lease_token_hash']}/{photo_id}.webp"
            slots[index] = {"photo_id": str(photo_id), "key": key, "spec": spec}
        await conn.execute("UPDATE jobs SET payload=$2::jsonb WHERE id=$1", job_id, json.dumps({"slots": slots}))
        ttl = max(1, min(settings.media_url_s, int((job["lease_expires_at"] - datetime.now(timezone.utc)).total_seconds())))
        uploads = []
        for frame in body.frames:
            slot = slots[str(frame.index)]
            uploads.append({"index": frame.index, "photo_id": slot["photo_id"],
                            "original_url": await storage.put_url(slot["key"], frame.original_bytes, ttl),
                            "thumbnail_url": await storage.put_url(thumb_key(slot["key"]), frame.thumbnail_bytes, ttl),
                            "content_type": "image/webp"})
        return {"sources": await signed_sources(conn, job, storage), "uploads": uploads}


def vector_text(vec):
    return "[" + ",".join(format(float(x), ".9g") for x in vec) + "]"


async def complete_job(conn, storage, job_id, body: Completion, *, cpu=False):
    digest = completion_digest(body)
    async with conn.transaction():
        job = await locked_lease(conn, job_id, body.lease_token, cpu=cpu, allow_done=True)
        if job["status"] == "done":
            if hmac.compare_digest(job["completion_digest"], digest):
                return {"status": "done", "outcome": job["terminal_outcome"], "replayed": True}
            raise HTTPException(409, "completion differs from committed result")
        if cpu and job["kind"] != "photo":
            raise HTTPException(409, "CPU cannot process video")
        # Same transaction-level lock as all resolvers and human verdicts.
        from app.matching import lock_matching, resolve_sighting
        await lock_matching(conn)
        sid = job["sighting_id"]
        sighting = await conn.fetchrow("SELECT * FROM sightings WHERE id=$1 FOR UPDATE", sid)
        if job["pipeline_version"] != body.pipeline_version:
            raise HTTPException(422, "pipeline mismatch")
        if job["kind"] == "photo":
            sources = {r["photo_id"]: r for r in await source_rows(conn, job)}
            if set(sources) != {f.photo_id for f in body.frames}:
                raise HTTPException(422, "completion must cover exactly the source photos")
            for frame in body.frames:
                source = sources[frame.photo_id]
                if frame.index is not None or any(getattr(frame, k) != source[k] for k in ("width", "height", "phash")):
                    raise HTTPException(422, "photo metadata differs from source")
        else:
            slots = payload_of(job).get("slots", {})
            if not 1 <= len(body.frames) <= MAX_FRAMES or len({f.index for f in body.frames}) != len(body.frames):
                raise HTTPException(422, "invalid frame count or duplicate indices")
            for frame in body.frames:
                slot = slots.get(str(frame.index))
                if slot is None or slot["photo_id"] != str(frame.photo_id):
                    raise HTTPException(422, "unknown frame upload slot")
                key = f"sightings/{sid}/{frame.photo_id}.webp"
                # Published keys are never PUT-signed. Conditional copy snapshots
                # the checked staging object, preventing late signed PUT mutation.
                for source_key, target_key, size in (
                    (slot["key"], key, slot["spec"]["original_bytes"]),
                    (thumb_key(slot["key"]), thumb_key(key), slot["spec"]["thumbnail_bytes"]),
                ):
                    await storage.publish_checked(source_key, target_key, size)
                await conn.execute("INSERT INTO photos (id,sighting_id,s3_key,width,height,phash) VALUES ($1,$2,$3,$4,$5,$6)", frame.photo_id, sid, key, frame.width, frame.height, frame.phash)
        vecs = []
        for frame in body.frames:
            if frame.vector is None:
                continue
            vecs.append(frame.vector)
            box = dict(zip(("x1", "y1", "x2", "y2"), frame.bbox))
            await conn.execute("""INSERT INTO embeddings (id,photo_id,model,dim,vec_miew,bbox)
                VALUES ($1,$2,$3,$4,$5::vector,$6::jsonb)
                ON CONFLICT(photo_id,model) DO UPDATE SET vec_miew=EXCLUDED.vec_miew,bbox=EXCLUDED.bbox,created_at=now()""",
                uuid7(), frame.photo_id, MODEL_NAME, EMBED_DIM, vector_text(frame.vector), json.dumps(box))
        mean = None
        if vecs:
            mean = np.mean(np.asarray(vecs, dtype=np.float64), axis=0)
            norm = float(np.linalg.norm(mean))
            if not math.isfinite(norm) or norm < 1e-8:
                raise HTTPException(422, "frame vectors cancel to zero")
            mean = vector_text(mean / norm)
        outcome = "ready" if vecs else "no_animal"
        await conn.execute("UPDATE sightings SET dog_confidence=$2,vec_miew=$3::vector,processing_state=$4,phash=COALESCE(phash,$5) WHERE id=$1", sid, max(f.dog_confidence for f in body.frames), mean, outcome, body.frames[0].phash)
        if vecs and sighting["review_status"] != "rejected":
            await resolve_sighting(conn, sid, auto_merge_min=settings.reid_auto_merge_min,
                propose_min=settings.reid_propose_min, radius_m=settings.reid_radius_m,
                max_candidates=settings.reid_max_candidates, new_uuid=uuid7,
                thin_evidence_frames=settings.reid_thin_evidence_frames)
        # now() is fixed at transaction start; wall-clock expiry is checked again
        # after S3 and matching so slow publication cannot revive an expired lease.
        live = await conn.fetchval("SELECT lease_expires_at>clock_timestamp() FROM jobs WHERE id=$1", job_id)
        if not live:
            raise HTTPException(409, "lease expired during completion")
        await conn.execute("UPDATE jobs SET status='done',completion_digest=$2,terminal_outcome=$3,updated_at=now() WHERE id=$1", job_id, digest, outcome)
        return {"status": "done", "outcome": outcome, "replayed": False}


@router.post("/{job_id}/complete")
async def complete(job_id: UUID, body: Completion, request: Request, storage=Depends(get_storage)):
    async with request.app.state.pool.acquire() as conn:
        return await complete_job(conn, storage, job_id, body)


async def fail_job(conn, job_id, body: Failure, *, cpu=False):
    async with conn.transaction():
        job = await locked_lease(conn, job_id, body.lease_token, cpu=cpu)
        retry = body.retryable and job["attempts"] < settings.media_max_attempts
        await conn.execute("""UPDATE jobs SET status=$2,last_error=$3,run_after=now()+$4*interval '1 second',
            lease_token_hash=NULL,lease_expires_at=NULL,terminal_outcome=$5,updated_at=now() WHERE id=$1""",
            job_id, "pending" if retry else "failed", body.error,
            min(300, 2 ** min(job["attempts"], 8)), None if retry else "failed")
        await conn.execute("UPDATE sightings SET processing_state=$2 WHERE id=$1", job["sighting_id"], "queued" if retry else "failed")
        return {"status": "queued" if retry else "failed"}


@router.post("/{job_id}/fail")
async def fail(job_id: UUID, body: Failure, request: Request):
    async with request.app.state.pool.acquire() as conn:
        return await fail_job(conn, job_id, body)


async def cpu_process(pool, storage, job, token):
    if job["kind"] != "photo":
        raise ValueError("CPU inference is photo-only")
    from app.analyse import analyse, embed_analysis
    async with pool.acquire() as conn:
        sources = await source_rows(conn, job)
    frames = []
    for source in sources:
        raw = await storage.get(source["s3_key"])
        analysis = await run_in_threadpool(analyse, raw)
        vector = await run_in_threadpool(embed_analysis, analysis)
        frames.append(FrameResult(photo_id=source["photo_id"], width=source["width"],
            height=source["height"], phash=source["phash"], dog_confidence=analysis.dog_confidence,
            cat_confidence=analysis.cat_confidence, bbox=analysis.box,
            vector=vector.tolist() if vector is not None else None))
    body = Completion(lease_token=token, pipeline_version=PIPELINE_VERSION, model=MODEL_NAME, frames=frames)
    async with pool.acquire() as conn:
        await complete_job(conn, storage, job["id"], body, cpu=True)


async def cpu_consumer(pool, storage):
    async def renew(job_id, token):
        while True:
            await asyncio.sleep(max(1, settings.media_lease_s / 3))
            async with pool.acquire() as conn:
                await heartbeat_job(conn, job_id, token, cpu=True)

    while True:
        try:
            async with pool.acquire() as conn:
                result = await claim_job(conn, "cpu", cpu=True)
            if result is None:
                await asyncio.sleep(settings.media_poll_s)
                continue
            job, token = result
            work = asyncio.create_task(cpu_process(pool, storage, job, token))
            heartbeat_task = asyncio.create_task(renew(job["id"], token))
            try:
                done, _ = await asyncio.wait((work, heartbeat_task), return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            except Exception:
                logger.warning("photo fallback job failed: %s", job["id"], exc_info=True)
                async with pool.acquire() as conn:
                    with suppress(HTTPException):
                        await fail_job(conn, job["id"], Failure(lease_token=token, error="CPU analysis failed"), cpu=True)
            finally:
                for task in (work, heartbeat_task):
                    task.cancel()
                await asyncio.gather(work, heartbeat_task, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("photo fallback queue unavailable; retrying", exc_info=True)
            await asyncio.sleep(settings.media_poll_s)
