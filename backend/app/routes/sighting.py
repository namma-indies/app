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
from app.detect import DOG_CONF_THRESHOLD
from app.detect_reid import animal_confidence
from app.ids import uuid7
from app.photos import process_photo, ProcessedPhoto
from app.storage.s3 import S3Storage
from app.video import extract_diverse_frames

logger = logging.getLogger(__name__)

router = APIRouter()


async def _max_dog_confidence(raws: list[bytes]) -> float | None:
    """Highest dog-confidence across the uploaded photos, or None if we
    couldn't score them. This is a label, never a gate -- the caller saves the
    sighting either way, so a detector failure costs us a label, not a photo."""
    best: float | None = None
    for raw in raws:
        try:
            # yolo26x, shared with the embedding path: the old yolov8n gate
            # scored visible dogs as low as 0.02 (yolo26x: 0.80 on the same
            # photo), and this task has never been on the user's wait path.
            conf, _cat = await run_in_threadpool(animal_confidence, raw)
        except Exception:
            logger.warning("dog detection failed; saving unscored", exc_info=True)
            continue
        best = conf if best is None else max(best, conf)
    return best


async def _embed_and_save(
    pool: asyncpg.Pool, sighting_id: UUID, photo_ids: list[UUID], raws: list[bytes]
) -> None:
    """Embed each photo for re-identification, after the sighting is saved.

    Same contract as dog-confidence scoring: this never gates the save. A photo
    with no embedding is simply not yet matchable -- it can be re-embedded later
    (the model is versioned in the row, so a re-run is an upsert). Losing the
    sighting because an embedder hiccuped would be the far worse trade.

    Photos with no dog detected are skipped rather than embedded whole-frame:
    an embedding of mostly-street would pollute candidate search with a vector
    that matches other streets.
    """
    from app.embed import EMBED_DIM, MODEL_NAME, embed_photo

    for photo_id, raw in zip(photo_ids, raws):
        try:
            found = await run_in_threadpool(embed_photo, raw)
        except Exception:
            logger.warning(
                "embedding failed for photo=%s; leaving unembedded",
                photo_id,
                exc_info=True,
            )
            continue
        if found is None:
            logger.info("no dog detected in photo=%s; not embedding", photo_id)
            continue
        vec, box = found
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO embeddings (id, photo_id, model, dim, vec_miew, bbox)
                    VALUES ($1, $2, $3, $4, $5::vector, $6::jsonb)
                    ON CONFLICT (photo_id, model) DO UPDATE
                        SET vec_miew = EXCLUDED.vec_miew,
                            bbox = EXCLUDED.bbox,
                            created_at = now()
                    """,
                    uuid7(),
                    photo_id,
                    MODEL_NAME,
                    EMBED_DIM,
                    "[" + ",".join(f"{float(v):.7g}" for v in vec) + "]",
                    json.dumps({"x1": box[0], "y1": box[1], "x2": box[2], "y2": box[3]}),
                )
        except Exception:
            logger.warning(
                "failed to store embedding for photo=%s", photo_id, exc_info=True
            )

    # Decide the match once, now that the vectors exist. This used to run inside
    # GET /sighting/{id}/match, which meant every read deleted and recreated the
    # pending proposals -- so a client that polled and then acted on a proposal
    # ID it had just been given got a 404. Resolving here makes the GET a pure
    # read, and matches the shape of the other background task: the response
    # already went out, nothing here can affect whether the sighting exists.
    from app.config import settings
    from app.matching import resolve_sighting

    try:
        async with pool.acquire() as conn:
            await resolve_sighting(
                conn,
                sighting_id,
                auto_merge_min=settings.reid_auto_merge_min,
                propose_min=settings.reid_propose_min,
                radius_m=settings.reid_radius_m,
                max_candidates=settings.reid_max_candidates,
                new_uuid=uuid7,
                thin_evidence_frames=settings.reid_thin_evidence_frames,
            )
    except Exception:
        logger.warning(
            "matching failed for sighting=%s; it stays unmatched and can be "
            "resolved by a re-run", sighting_id, exc_info=True
        )


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
    photos: list[UploadFile] | None = File(None),
    video: UploadFile | None = File(None),
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
    if not photos and video is None:
        raise HTTPException(
            status_code=422, detail="at least one photo or a video is required"
        )
    if photos and video is not None:
        raise HTTPException(
            status_code=422, detail="provide either photos or a video, not both"
        )

    sighting_id = uuid7()

    processed_frames: list[ProcessedPhoto]
    if video is not None:
        try:
            # Decoding is CPU-bound and can run for seconds on a long clip;
            # off the event loop so it does not stall every other request.
            processed_frames = await run_in_threadpool(
                extract_diverse_frames, await video.read()
            )
        except (ValueError, OSError, RuntimeError):
            raise HTTPException(
                status_code=422, detail="could not read video / no decodable frames"
            )
        # The raw video is never persisted -- only the frames it yielded. Those
        # frames are also what the background tasks see, so dog-confidence and
        # the embedding score exactly the bytes we stored.
        raws = [p.original for p in processed_frames]
    else:
        # Read once. An UploadFile is a stream: reading it a second time yields
        # b"", so `raws` has to be the single source for both the stored photo
        # and the background tasks.
        raws = [await f.read() for f in photos]
        processed_frames = [process_photo(raw) for raw in raws]

    photo_rows = []
    first_phash: str | None = None
    for p in processed_frames:
        photo_id = uuid7()
        if first_phash is None:
            first_phash = p.phash
        orig_key = f"sightings/{sighting_id}/{photo_id}.webp"
        thumb_key = f"sightings/{sighting_id}/{photo_id}_thumb.webp"
        await storage.put(orig_key, p.original, p.content_type)
        await storage.put(thumb_key, p.thumbnail, p.content_type)
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
    if video is not None:
        attrs["source"] = "video"

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
    background_tasks.add_task(
        _embed_and_save,
        request.app.state.pool,
        sighting_id,
        [r["id"] for r in photo_rows],
        raws,
    )

    return JSONResponse(
        status_code=201,
        content={
            "sighting_id": str(sighting_id),
            "photo_ids": [str(r["id"]) for r in photo_rows],
        },
    )
