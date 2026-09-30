"""Private inspection of persisted evidence, independent of publication and inference."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query
from itsdangerous import BadData, URLSafeSerializer

from app.auth.deps import require_moderator
from app.captures import decoded
from app.config import settings
from app.deps import get_conn, get_storage
from app.detect_reid import DETECTOR_NAME
from app.photos import thumb_key

router = APIRouter(prefix="/moderation/animal-audit")
AuditFilter = Literal["all", "needs_review", "reviewed", "no_detection"]
AuditKind = Literal["sighting", "capture"]

# Capture rows disappear as soon as any sighting is published. Source photos
# are not sighting scores: siblings share a source but never a verdict or score.
AUDIT_ROWS = """
    WITH entries AS (
        SELECT s.id, 'sighting'::text AS kind, s.created_at, s.captured_at,
               s.processing_state, s.animal_override,
               COALESCE(s.clip_s3_key, c.clip_s3_key) AS clip_s3_key
        FROM sightings s LEFT JOIN captures c ON c.id = s.capture_id
        UNION ALL
        SELECT c.id, 'capture'::text, c.created_at, c.captured_at,
               c.processing_state, c.animal_override, c.clip_s3_key
        FROM captures c
        WHERE c.processing_state <> 'legacy'
          AND NOT EXISTS (SELECT 1 FROM sightings s WHERE s.capture_id = c.id)
    )
    SELECT e.*, pd.dog, pd.cat, pd.s3_key
    FROM entries e
    LEFT JOIN LATERAL (
        SELECT max(d.dog) AS dog, max(d.cat) AS cat,
               (array_agg(p.s3_key ORDER BY greatest(d.dog, d.cat) DESC NULLS LAST,
                                           p.created_at, p.id))[1] AS s3_key
        FROM photos p
        LEFT JOIN detections d ON d.photo_id = p.id AND d.model = $1
        WHERE (e.kind = 'sighting' AND p.sighting_id = e.id)
           OR (e.kind = 'capture' AND p.capture_id = e.id)
    ) pd ON TRUE
"""


def _serializer():
    return URLSafeSerializer(settings.session_secret, salt="animal-audit-v1")


def _cursor(row, filter):
    return _serializer().dumps({"v": 1, "filter": filter,
        "created_at": row["created_at"].isoformat(), "id": str(row["id"]), "kind": row["kind"]})


def _read_cursor(before, filter):
    if before is None:
        return None, None, None
    try:
        value = _serializer().loads(before)
        if value["v"] != 1 or value["filter"] != filter or value["kind"] not in ("sighting", "capture"):
            raise ValueError
        created_at = datetime.fromisoformat(value["created_at"])
        if created_at.tzinfo is None:
            raise ValueError
        return created_at, UUID(value["id"]), value["kind"]
    except (BadData, ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(422, "invalid audit cursor") from None


def _item(row, urls):
    return {"id": str(row["id"]), "kind": row["kind"], "captured_at": row["captured_at"],
        "processing_state": row["processing_state"], "animal_override": row["animal_override"],
        "dog": row["dog"], "cat": row["cat"],
        "thumb_url": urls.get(thumb_key(row["s3_key"])) if row["s3_key"] else None}


async def _signed(storage, keys):
    keys = list(dict.fromkeys(keys))
    return dict(zip(keys, await storage.urls(keys))) if keys else {}


@router.get("")
async def animal_audit(
    filter: AuditFilter = "all",
    before: str | None = Query(None, max_length=2048),
    limit: int = Query(20, ge=1, le=100),
    _mod: UUID = Depends(require_moderator),
    conn=Depends(get_conn), storage=Depends(get_storage),
):
    created_at, item_id, kind = _read_cursor(before, filter)
    rows = await conn.fetch(AUDIT_ROWS + """
        WHERE ($2 = 'all'
            OR ($2 = 'reviewed' AND e.animal_override IS NOT NULL)
            OR ($2 = 'no_detection' AND (
                e.processing_state = 'no_animal'
                OR greatest(pd.dog, pd.cat) = 0
                OR (pd.dog IS NULL AND pd.cat IS NULL)))
            OR ($2 = 'needs_review' AND e.animal_override IS NULL AND (
                greatest(pd.dog, pd.cat) IS NULL OR greatest(pd.dog, pd.cat) < $3::real
                OR e.processing_state IN ('needs_review', 'no_animal', 'failed'))))
          AND ($4::timestamptz IS NULL OR (e.created_at, e.id, e.kind) < ($4, $5::uuid, $6::text))
        ORDER BY e.created_at DESC, e.id DESC, e.kind DESC LIMIT $7
        """, DETECTOR_NAME, filter, settings.animal_review_max, created_at, item_id, kind, limit + 1)
    page = rows[:limit]
    urls = await _signed(storage, [thumb_key(r["s3_key"]) for r in page if r["s3_key"]])
    return {"items": [_item(r, urls) for r in page],
        "next_cursor": _cursor(page[-1], filter) if len(rows) > limit else None}


def _box(instance, *, crop=False):
    bbox = decoded(instance["bbox"])
    if crop:
        x, y, _, _ = decoded(instance["crop_bbox"])
        bbox = [bbox[0] - x, bbox[1] - y, bbox[2] - x, bbox[3] - y]
    return {"bbox": bbox, "species": instance["species"], "confidence": instance["confidence"]}


@router.get("/{kind}/{item_id}")
async def animal_audit_detail(
    kind: AuditKind, item_id: UUID,
    _mod: UUID = Depends(require_moderator),
    conn=Depends(get_conn), storage=Depends(get_storage),
):
    # Publication writes sources, instances and sightings in one transaction;
    # keep this read on a single snapshot rather than mixing either side of it.
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        row = await conn.fetchrow(AUDIT_ROWS + " WHERE e.kind = $2 AND e.id = $3",
            DETECTOR_NAME, kind, item_id)
        if row is None:
            raise HTTPException(404, "no such audit item")
        # All photos are reachable: <=96 crops +48 sources for v2, <=12 for
        # legacy intake. Do not silently truncate the evidence for a verdict.
        photos = await conn.fetch("""
            SELECT p.id, p.s3_key, p.width, p.height, d.dog, d.cat, d.model,
                   CASE WHEN EXISTS (SELECT 1 FROM animal_instances a WHERE a.evidence_photo_id = p.id)
                        THEN 'crop' ELSE 'source' END AS evidence_kind
            FROM photos p
            LEFT JOIN detections d ON d.photo_id = p.id AND d.model = $1
            WHERE ($2 = 'capture' AND p.capture_id = $3)
               OR ($2 = 'sighting' AND (p.sighting_id = $3 OR EXISTS (
                    SELECT 1 FROM animal_instances a
                    WHERE a.sighting_id = $3 AND a.source_photo_id = p.id)))
            ORDER BY EXISTS (SELECT 1 FROM animal_instances a WHERE a.evidence_photo_id = p.id),
                     p.created_at, p.id
            """, DETECTOR_NAME, kind, item_id)
        instances = await conn.fetch("""
            SELECT source_photo_id, evidence_photo_id, bbox, crop_bbox, species, confidence
            FROM animal_instances
            WHERE (source_photo_id = ANY($1::uuid[]) OR evidence_photo_id = ANY($1::uuid[]))
              AND ($2 = 'capture' OR sighting_id = $3)
            ORDER BY id
            """, [p["id"] for p in photos], kind, item_id)
    keys = [key for p in photos for key in (p["s3_key"], thumb_key(p["s3_key"]))]
    if row["s3_key"]:
        keys.append(thumb_key(row["s3_key"]))
    if row["clip_s3_key"]:
        keys.append(row["clip_s3_key"])
    urls = await _signed(storage, keys)
    boxes = {}
    for instance in instances:
        boxes.setdefault(instance["source_photo_id"], []).append(_box(instance))
        boxes.setdefault(instance["evidence_photo_id"], []).append(_box(instance, crop=True))
    return {**_item(row, urls), "clip_url": urls.get(row["clip_s3_key"]), "evidence": [
        {"id": str(p["id"]), "evidence_kind": p["evidence_kind"], "photo_url": urls[p["s3_key"]],
         "thumb_url": urls[thumb_key(p["s3_key"])], "width": p["width"], "height": p["height"],
         "dog": p["dog"], "cat": p["cat"], "model": p["model"], "boxes": boxes.get(p["id"], [])}
        for p in photos]}


@router.post("/capture/{capture_id}")
async def rule_on_capture_animal(
    capture_id: UUID, verdict: Literal["animal", "no_animal"] = Form(...),
    moderator_id: UUID = Depends(require_moderator), conn=Depends(get_conn),
):
    async with conn.transaction():
        # Both worker completion and owner publication take this same row lock.
        # Recheck after waiting so a newly published capture cannot be reviewed
        # as if the verdict applied to its new, independently reviewed sightings.
        capture = await conn.fetchrow("SELECT processing_state FROM captures WHERE id=$1 FOR UPDATE", capture_id)
        if capture is None or capture["processing_state"] == "legacy":
            raise HTTPException(404, "no such audit capture")
        if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM sightings WHERE capture_id=$1)", capture_id):
            raise HTTPException(409, "capture has published sightings; reload and review each sighting")
        await conn.execute("""UPDATE captures SET animal_override=$2,
            animal_reviewed_at=clock_timestamp(), animal_reviewed_by=$3 WHERE id=$1""",
            capture_id, verdict == "animal", moderator_id)
    return {"status": "ok", "animal_override": verdict == "animal"}
