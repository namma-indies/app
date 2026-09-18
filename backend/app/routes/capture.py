import json
import math
from datetime import datetime
from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from starlette.concurrency import run_in_threadpool

from app.auth.deps import require_observer
from app.capture_contracts import CaptureReceipt, CaptureReview, CaptureStatus, MULTI_PIPELINE_VERSION
from app.captures import publish_groups, receipt, status, validate_groups
from app.config import settings
from app.deps import get_conn, get_storage
from app.ids import uuid7
from app.photos import process_photo, thumb_key
from app.routes.sighting import _read_bounded

router = APIRouter()


async def owned(conn, capture_id, observer_id, *, lock=False):
    row = await conn.fetchrow("SELECT * FROM captures WHERE id=$1 AND observer_id=$2" +
        (" FOR UPDATE" if lock else ""),capture_id,observer_id)
    if row is None:
        raise HTTPException(404, "no such capture")
    return row


@router.get("/captures", response_model=dict[str, list[CaptureStatus]])
async def list_captures(observer_id: UUID = Depends(require_observer), conn=Depends(get_conn),
        storage=Depends(get_storage), limit: int = Query(50, ge=1, le=100),
        before: datetime | None = None):
    rows = await conn.fetch("""SELECT * FROM captures WHERE observer_id=$1
        AND processing_state <> 'legacy' AND ($3::timestamptz IS NULL OR created_at<$3)
        ORDER BY created_at DESC,id DESC LIMIT $2""", observer_id,limit,before)
    return {"items": [await status(conn,storage,r) for r in rows]}


@router.get("/capture/{capture_id}", response_model=CaptureStatus)
async def get_capture(capture_id: UUID, observer_id: UUID = Depends(require_observer),
        conn=Depends(get_conn), storage=Depends(get_storage)):
    return await status(conn,storage,await owned(conn,capture_id,observer_id))


@router.post("/capture/{capture_id}/review", response_model=CaptureStatus)
async def review_capture(capture_id: UUID, body: CaptureReview,
        observer_id: UUID = Depends(require_observer), conn=Depends(get_conn), storage=Depends(get_storage)):
    from app.matching import lock_matching
    async with conn.transaction():
        await lock_matching(conn)
        capture = await owned(conn,capture_id,observer_id,lock=True)
        if capture["revision"] != body.revision:
            raise HTTPException(409, "capture revision changed; reload before reviewing")
        if capture["processing_state"] not in ("needs_review", "ready"):
            raise HTTPException(409, "capture is not ready for association review")
        rows = await conn.fetch("SELECT * FROM animal_instances WHERE capture_id=$1",capture_id)
        validate_groups(rows,body.groups)
        if body.publish:
            await publish_groups(conn,capture,body.groups)
        else:
            if capture["published_at"]:
                raise HTTPException(409,"published captures cannot return to draft")
            await conn.execute("UPDATE captures SET review_groups=$2::jsonb WHERE id=$1",capture_id,
                json.dumps([g.model_dump(mode="json") for g in body.groups]))
        await conn.execute("UPDATE captures SET revision=revision+1,updated_at=now() WHERE id=$1",capture_id)
    return await status(conn,storage,await owned(conn,capture_id,observer_id))


@router.post("/capture", response_model=CaptureReceipt, status_code=201)
async def create_capture(request: Request, photos: list[UploadFile] | None = File(None),
        video: UploadFile | None = File(None), lat: float | None = Form(None),
        lng: float | None = Form(None), geo_accuracy_m: float | None = Form(None),
        client_token: str | None = Form(None, max_length=200),
        geo_source: Literal["device_gps", "pin", "none", "exif"] = Form(...),
        captured_at: datetime = Form(...), reported_at: datetime | None = Form(None),
        note: str | None = Form(None, max_length=4000),
        observer_id: UUID = Depends(require_observer), storage=Depends(get_storage)):
    pool = request.app.state.pool
    # A disabled intake must still acknowledge a previously accepted upload.
    if client_token:
        async with pool.acquire() as conn:
            old = await conn.fetchrow("SELECT * FROM captures WHERE observer_id=$1 AND client_token=$2",observer_id,client_token)
            if old:
                return await receipt(conn,old,duplicate=True)
    if not (settings.multi_animal_enabled and settings.media_jobs_enabled):
        raise HTTPException(404,"multi-animal capture intake is disabled")
    if bool(photos) == (video is not None):
        raise HTTPException(422,"provide either photos or a video")
    if photos and len(photos) > 12:
        raise HTTPException(422,"at most 12 photos allowed")
    if lat is not None and not -90 <= lat <= 90:
        raise HTTPException(422,"invalid latitude")
    if lng is not None and not -180 <= lng <= 180:
        raise HTTPException(422,"invalid longitude")
    if geo_accuracy_m is not None and (not math.isfinite(geo_accuracy_m) or geo_accuracy_m < 0):
        raise HTTPException(422,"invalid accuracy")
    capture_id = uuid7()
    rows = []
    clip_key = None
    if video is not None:
        raw = await _read_bounded(video,settings.media_max_video_bytes)
        clip_key = f"captures/{capture_id}/clip.mp4"
        await storage.put(clip_key,raw,video.content_type or "video/mp4")
    else:
        total_bytes = 0
        for upload in photos:
            raw = await _read_bounded(upload,settings.media_max_photo_bytes)
            total_bytes += len(raw)
            if total_bytes > 256 * 1024 * 1024:
                raise HTTPException(413,"capture exceeds aggregate photo byte budget")
            try:
                p = await run_in_threadpool(process_photo,raw)
            except Exception:
                raise HTTPException(422,"could not read one of the photos")
            pid = uuid7()
            key = f"captures/{capture_id}/sources/{pid}.webp"
            await storage.put(key,p.original,p.content_type)
            await storage.put(thumb_key(key),p.thumbnail,p.content_type)
            rows.append((pid,key,p.width,p.height,p.phash))
    attrs = {"note":note} if note else {}
    if video is not None:
        attrs["source"] = "video"
    has_geo = geo_source != "none" and lat is not None and lng is not None
    try:
        async with pool.acquire() as conn, conn.transaction():
            capture = await conn.fetchrow("""INSERT INTO captures(id,observer_id,client_token,kind,
                captured_at,reported_at,geog,geo_source,geo_accuracy_m,attrs,clip_s3_key)
                VALUES($1,$2,$3,$4,$5,$6,
                    CASE WHEN $7::bool THEN ST_SetSRID(ST_MakePoint($8,$9),4326)::geography ELSE NULL END,
                    $10,$11,$12::jsonb,$13) RETURNING *""",capture_id,observer_id,client_token,
                "video" if video is not None else "photo",captured_at,reported_at,has_geo,lng,lat,
                geo_source,geo_accuracy_m,json.dumps(attrs),clip_key)
            for pid,key,width,height,phash in rows:
                await conn.execute("""INSERT INTO photos(id,capture_id,s3_key,width,height,phash)
                    VALUES($1,$2,$3,$4,$5,$6)""",pid,capture_id,key,width,height,phash)
            await conn.execute("""INSERT INTO jobs(id,kind,capture_id,pipeline_version,run_after,cpu_eligible_at)
                VALUES($1,$2,$3,$4,now(),now()+$5*interval '1 second')""",uuid7(),
                "video" if video is not None else "photo",capture_id,MULTI_PIPELINE_VERSION,settings.media_cpu_grace_s)
            return await receipt(conn,capture)
    except asyncpg.UniqueViolationError:
        async with pool.acquire() as conn:
            winner = await conn.fetchrow("SELECT * FROM captures WHERE observer_id=$1 AND client_token=$2",observer_id,client_token)
            if winner is None:
                raise
            return await receipt(conn,winner,duplicate=True)
