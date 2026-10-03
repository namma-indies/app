"""Moderator-only, identity-independent match verification experiments."""
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app import match_calibration as calibration
from app.auth.deps import require_moderator
from app.deps import get_conn, get_storage
from app.photos import thumb_key

router = APIRouter(prefix="/moderation/match-calibration")


class BatchRequest(BaseModel):
    sample_size: int = Field(default=100, ge=1, le=500)


class LabelRequest(BaseModel):
    verdict: Literal["same", "different", "unsure"]
    evidence_token: str = Field(min_length=1, max_length=128)


@router.get("")
async def list_batches(offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
                       _mod: UUID = Depends(require_moderator), conn=Depends(get_conn)):
    rows = await conn.fetch(f"SELECT {calibration.BATCH_FIELDS} FROM match_calibration_batches "
                            "ORDER BY created_at DESC,id DESC OFFSET $1 LIMIT $2", offset, limit + 1)
    return {"batches": [dict(r) for r in rows[:limit]],
            "next_offset": offset + limit if len(rows) > limit else None}


@router.post("", status_code=201)
async def create_batch(body: BatchRequest, moderator: UUID = Depends(require_moderator), conn=Depends(get_conn)):
    return await calibration.create_batch(conn, moderator, body.sample_size)


@router.get("/{batch_id}/pairs")
async def list_pairs(batch_id: UUID, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
                     moderator: UUID = Depends(require_moderator), conn=Depends(get_conn)):
    async with calibration.snapshot(conn):
        batch = await calibration.batch(conn, batch_id)
        pairs = await calibration.validated_pairs(conn, batch, moderator)
    page = pairs[offset:offset+limit]
    return {"items": [calibration.pair_item(p) for p in page],
            "next_offset": offset+limit if offset+limit < len(pairs) else None}


def _side(evidence, urls):
    photos = []
    for photo in evidence["photos"]:
        bbox = photo["bbox"]
        if isinstance(bbox, dict):
            bbox = [bbox[key] for key in ("x1", "y1", "x2", "y2")] if all(
                key in bbox for key in ("x1", "y1", "x2", "y2")
            ) else None
        instance = photo["instance"]
        if instance is not None:
            x, y, _, _ = instance["crop_bbox"]
            a, b, c, d = instance["bbox"]
            bbox = [a-x, b-y, c-x, d-y]
        photos.append({"id": photo["id"], "photo_url": urls[photo["s3_key"]],
            "thumb_url": urls[thumb_key(photo["s3_key"])], "width": photo["width"],
            "height": photo["height"], "bbox": bbox})
    return {"sighting_id": evidence["id"], "species": evidence["species"], "photos": photos}


@router.get("/{batch_id}/pairs/{pair_id}")
async def pair_detail(batch_id: UUID, pair_id: UUID, moderator: UUID = Depends(require_moderator),
                      conn=Depends(get_conn), storage=Depends(get_storage)):
    async with calibration.snapshot(conn):
        batch = await calibration.batch(conn, batch_id)
        pair = (await calibration.validated_pairs(conn, batch, moderator, pair_id))[0]
    manifest = pair["manifest"]
    if pair["status"] == "stale":
        # A frozen key is not an enduring right to access deleted, reassigned,
        # or replaced evidence. Do not mint fresh URLs from a stale snapshot.
        return {**calibration.pair_item(pair), "evidence_token": pair["evidence_hash"],
                **{side: {"sighting_id": manifest[side]["id"], "species": manifest[side]["species"],
                           "photos": []} for side in ("left", "right")}}
    keys = list(dict.fromkeys(key for side in manifest.values() for photo in side["photos"]
                            for key in (photo["s3_key"], thumb_key(photo["s3_key"]))))
    urls = dict(zip(keys, await storage.urls(keys))) if keys else {}
    return {**calibration.pair_item(pair), "evidence_token": pair["evidence_hash"],
            "left": _side(manifest["left"], urls), "right": _side(manifest["right"], urls)}


@router.post("/{batch_id}/pairs/{pair_id}/label")
async def label_pair(batch_id: UUID, pair_id: UUID, body: LabelRequest,
                     moderator: UUID = Depends(require_moderator), conn=Depends(get_conn)):
    async with calibration.snapshot(conn, label=True):
        batch = await calibration.batch(conn, batch_id)
        await conn.fetchrow("SELECT id FROM match_calibration_pairs WHERE id=$1 AND batch_id=$2 FOR UPDATE NOWAIT",
                            pair_id, batch_id)
        pair = (await calibration.validated_pairs(conn, batch, moderator, pair_id))[0]
        stale = pair["status"] == "stale" or body.evidence_token != pair["evidence_hash"]
        if not stale:
            await conn.execute("""INSERT INTO match_calibration_labels(pair_id,moderator_id,verdict)
                VALUES($1,$2,$3) ON CONFLICT(pair_id,moderator_id)
                DO UPDATE SET verdict=EXCLUDED.verdict,updated_at=clock_timestamp()""",
                pair_id, moderator, body.verdict)
    # Commit stale flags even when a label is refused.
    if stale:
        raise HTTPException(409, "stale calibration evidence; reload or skip this pair")
    return {"status": "ok"}


@router.get("/{batch_id}/metrics")
async def batch_metrics(batch_id: UUID, moderator: UUID = Depends(require_moderator), conn=Depends(get_conn)):
    async with calibration.snapshot(conn):
        batch = await calibration.batch(conn, batch_id)
        pairs = await calibration.validated_pairs(conn, batch, moderator)
    return calibration.metrics(batch, pairs)
