import json
import logging
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
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


async def _max_dog_confidence(raws: list[bytes]) -> float:
    """Highest dog-confidence across the uploaded photos. Fails open: any
    detector error (bad image, model issue) returns 1.0 so a broken gate never
    blocks a legitimate capture."""
    best = 0.0
    for raw in raws:
        try:
            best = max(best, await run_in_threadpool(dog_confidence, raw))
        except Exception:
            logger.warning("dog detection failed; failing open", exc_info=True)
            return 1.0
    return best


@router.post("/sighting")
async def create_sighting(
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
    override_no_dog: bool = Form(False),
    observer_id: UUID = Depends(require_observer),
    conn=Depends(get_conn),
    storage: S3Storage = Depends(get_storage),
):
    if not photos:
        raise HTTPException(status_code=422, detail="at least one photo is required")

    raws = [await f.read() for f in photos]

    # Dog-presence gate. High-recall: block only when no photo shows a dog, and
    # let the user override ("save anyway"). Fails open on detector errors.
    if not override_no_dog:
        conf = await _max_dog_confidence(raws)
        if conf < DOG_CONF_THRESHOLD:
            return JSONResponse(
                status_code=422,
                content={"reason": "no_dog", "confidence": round(conf, 3)},
            )

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
                     phash, attrs)
                VALUES
                    ($1, $2, $3, $4,
                     ST_SetSRID(ST_MakePoint($5, $6), 4326)::geography,
                     $7, $8, NULL, 'unmatched', 'valid', $9, $10::jsonb)
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
            )
        else:
            await conn.execute(
                """
                INSERT INTO sightings
                    (id, observer_id, captured_at, reported_at, geog, geo_source,
                     geo_accuracy_m, individual_id, match_status, review_status,
                     phash, attrs)
                VALUES
                    ($1, $2, $3, $4, NULL, $5, $6, NULL, 'unmatched', 'valid', $7, $8::jsonb)
                """,
                sighting_id,
                observer_id,
                captured_at,
                reported_at,
                geo_source,
                geo_accuracy_m,
                first_phash,
                json.dumps(attrs),
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

    return JSONResponse(
        status_code=201,
        content={
            "sighting_id": str(sighting_id),
            "photo_ids": [str(r["id"]) for r in photo_rows],
        },
    )
