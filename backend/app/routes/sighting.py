import json
import logging
from datetime import datetime
from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from app.auth.deps import require_observer
from app.deps import get_conn, get_storage
from app.detect import DOG_CONF_THRESHOLD, dog_confidence
from app.ids import uuid7
from app.photos import process_photo
from app.storage.s3 import S3Storage

logger = logging.getLogger(__name__)

router = APIRouter()


async def _max_dog_confidence(raws: list[bytes]) -> float | None:
    """Highest dog-confidence across the uploaded photos, or None if we
    couldn't score them. This is a label, never a gate -- the caller saves the
    sighting either way, so a detector failure costs us a label, not a photo."""
    best: float | None = None
    for raw in raws:
        try:
            conf = await run_in_threadpool(dog_confidence, raw)
        except Exception:
            logger.warning("dog detection failed; saving unscored", exc_info=True)
            continue
        best = conf if best is None else max(best, conf)
    return best


async def _score_and_save_dog_confidence(
    pool: asyncpg.Pool, sighting_id: UUID, raws: list[bytes]
) -> None:
    """Runs after the sighting is already saved. Scoring is a label, never a
    gate, so a failure here (bad image, model error) just leaves
    dog_confidence NULL -- it must never affect whether the sighting exists."""
    dog_conf = await _max_dog_confidence(raws)
    if dog_conf is None:
        return
    if dog_conf < DOG_CONF_THRESHOLD:
        logger.info(
            "low dog confidence, saved anyway: conf=%.3f threshold=%.2f sighting=%s",
            dog_conf,
            DOG_CONF_THRESHOLD,
            sighting_id,
        )
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE sightings SET dog_confidence=$1 WHERE id=$2", dog_conf, sighting_id
            )
    except Exception:
        logger.warning(
            "failed to save dog_confidence for sighting=%s", sighting_id, exc_info=True
        )


@router.post("/sighting")
async def create_sighting(
    background_tasks: BackgroundTasks,
    request: Request,
    photos: list[UploadFile] = File(...),
    lat: float | None = Form(None),
    lng: float | None = Form(None),
    geo_accuracy_m: float | None = Form(None),
    geo_source: Literal["device_gps", "pin", "none"] = Form(...),
    captured_at: datetime = Form(...),
    reported_at: datetime | None = Form(None),
    note: str | None = Form(None),
    sex: Literal["male", "female", "unsure"] | None = Form(None),
    ear_notch: Literal["none", "left", "right", "unsure"] | None = Form(None),
    condition: Literal["healthy", "injured", "unsure"] | None = Form(None),
    # Accepted and ignored: queued offline captures still carry this field, and
    # rejecting them on replay would strand exactly the sightings we promised
    # to keep. Remove once no client in the field sends it.
    override_no_dog: bool = Form(False),
    observer_id: UUID = Depends(require_observer),
    conn=Depends(get_conn),
    storage: S3Storage = Depends(get_storage),
):
    if not photos:
        raise HTTPException(status_code=422, detail="at least one photo is required")

    raws = [await f.read() for f in photos]

    sighting_id = uuid7()

    photo_rows = []
    first_phash: str | None = None
    for raw in raws:
        p = process_photo(raw)
        photo_id = uuid7()
        if first_phash is None:
            first_phash = p.phash
        orig_key = f"sightings/{sighting_id}/{photo_id}.jpg"
        thumb_key = f"sightings/{sighting_id}/{photo_id}_thumb.jpg"
        await storage.put(orig_key, p.original, "image/jpeg")
        await storage.put(thumb_key, p.thumbnail, "image/jpeg")
        photo_rows.append(
            {
                "id": photo_id,
                "s3_key": orig_key,
                "width": p.width,
                "height": p.height,
                "phash": p.phash,
            }
        )

    geog_present = geo_source != "none" and lat is not None and lng is not None
    attrs = {
        k: v
        for k, v in {
            "note": note,
            "sex": sex,
            "ear_notch": ear_notch,
            "condition": condition,
        }.items()
        if v
    }

    async with conn.transaction():
        if geog_present:
            await conn.execute(
                """
                INSERT INTO sightings
                    (id, observer_id, captured_at, reported_at, geog, geo_source,
                     geo_accuracy_m, individual_id, match_status, review_status,
                     phash, attrs, dog_confidence)
                VALUES
                    ($1, $2, $3, $4,
                     ST_SetSRID(ST_MakePoint($5, $6), 4326)::geography,
                     $7, $8, NULL, 'unmatched', 'valid', $9, $10::jsonb, $11)
                """,
                sighting_id,
                observer_id,
                captured_at,
                reported_at,
                lng,
                lat,
                geo_source,
                geo_accuracy_m,
                first_phash,
                json.dumps(attrs),
                None,
            )
        else:
            await conn.execute(
                """
                INSERT INTO sightings
                    (id, observer_id, captured_at, reported_at, geog, geo_source,
                     geo_accuracy_m, individual_id, match_status, review_status,
                     phash, attrs, dog_confidence)
                VALUES
                    ($1, $2, $3, $4, NULL, $5, $6, NULL, 'unmatched', 'valid',
                     $7, $8::jsonb, $9)
                """,
                sighting_id,
                observer_id,
                captured_at,
                reported_at,
                geo_source,
                geo_accuracy_m,
                first_phash,
                json.dumps(attrs),
                None,
            )

        for row in photo_rows:
            await conn.execute(
                """
                INSERT INTO photos (id, sighting_id, s3_key, width, height, phash)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                row["id"],
                sighting_id,
                row["s3_key"],
                row["width"],
                row["height"],
                row["phash"],
            )

    background_tasks.add_task(
        _score_and_save_dog_confidence, request.app.state.pool, sighting_id, raws
    )

    return JSONResponse(
        status_code=201,
        content={
            "sighting_id": str(sighting_id),
            "photo_ids": [str(r["id"]) for r in photo_rows],
        },
    )
