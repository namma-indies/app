"""Screening an upload before anyone else sees it, and the held-upload queue (#181).

WHY
---
Apple's Guideline 1.2 asks for a way to filter objectionable material before
it is posted. Before this, the only automatic check was the animal detector,
whose public threshold ships inert, so a photo of a person, a face or the
inside of someone's home went straight to the shared map (#54).

HOW
---
Every new upload starts `pending` (migration 0034) and is off every surface
anyone else sees until `screen` has run over what the detector stored for it.
That either clears it, and it goes public as before, or holds it with its
reasons, and it waits here for a moderator to approve or reject it. The owner
sees their own upload throughout (`/dex` says why it is not on the map).

The rules, from the YOLO pass every upload already gets (`app/scene.py`):

* `person`     a confident person box covers `screening_person_area_min` of a frame
* `indoor`     indoor-only furniture scores `screening_indoor_min`
* `new_account` fewer than `screening_trusted_after` of the account's uploads
               have gone public; moderators are exempt from this one rule
* `unscreened` the detector failed on a photo, so nothing is known about it
* `model_*`    the local model, when on, saw something the detector cannot
               (app/model_screening.py; it can only add a hold)

FAILS SAFE
----------
A detector failure holds, and an upload whose screening never finishes stays
hidden: after `screening_stale_s` it reaches the queue as "not screened" so a
person can release it. Signals a worker does not report yet (NULL) are "not
measured" rather than a failure, so the API can be deployed before the worker.

Kept apart from `review_status`, which is about reports: approving a held
upload must not make it immune to later reports, and a report ruling sets
`reviewed_at`, which is what stops reports hiding it.
"""

import logging
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.deps import require_moderator
from app.config import settings
from app.deps import get_conn, get_storage
from app.detect_reid import DETECTOR_NAME
from app.photos import thumb_key
from app.storage.s3 import S3Storage

logger = logging.getLogger(__name__)
router = APIRouter()

# Only these reach anyone but the owner and moderators (aggregates.screened).
PUBLIC = ("clear", "approved")
MAX_QUEUE = 200
# Whole frames shown per held upload: enough to judge, few enough to load.
MAX_PHOTOS = 4

# An upload's whole frames, oldest first: the photos of its sightings and its
# own sources, never the animal crops cut from them -- the question is what
# the photo shows, and a crop is cut to leave the rest of it out.
FRAME_KEYS = """ARRAY(SELECT p.s3_key FROM photos p
    WHERE (p.capture_id = {capture} OR p.sighting_id IN (SELECT id FROM sightings WHERE capture_id = {capture}))
      AND NOT EXISTS (SELECT 1 FROM animal_instances a WHERE a.evidence_photo_id = p.id)
    ORDER BY p.created_at, p.id LIMIT {limit})"""


async def start(conn, capture_id: UUID) -> None:
    """Mark a just-created upload unscreened. Call in the transaction that
    creates the capture, so it is never public before `screen` runs."""
    if settings.screening_enabled:
        await conn.execute(
            "UPDATE captures SET screening = 'pending', hold_reasons = '{}' WHERE id = $1", capture_id)


async def reasons_for(conn, capture_id: UUID, observer_id: UUID | None) -> list[str]:
    """Why this upload should wait for a moderator; empty when it need not."""
    photos = await conn.fetch(
        """SELECT d.photo_id IS NOT NULL AS scored, d.person_area, d.indoor
           FROM photos p
           LEFT JOIN detections d ON d.photo_id = p.id AND d.model = $2
           WHERE p.capture_id = $1
              OR p.sighting_id IN (SELECT id FROM sightings WHERE capture_id = $1)""",
        capture_id, DETECTOR_NAME)
    reasons = []
    if not photos or not all(p["scored"] for p in photos):
        reasons.append("unscreened")
    if any((p["person_area"] or 0) >= settings.screening_person_area_min for p in photos):
        reasons.append("person")
    if any((p["indoor"] or 0) >= settings.screening_indoor_min for p in photos):
        reasons.append("indoor")
    if observer_id is not None and settings.screening_trusted_after > 0:
        row = await conn.fetchrow(
            """SELECT o.trust_tier = 'moderator' AS moderator,
                      (SELECT count(*) FROM captures c
                       WHERE c.observer_id = o.id AND c.id <> $2 AND c.screening IN ('clear','approved')) AS shown
               FROM observers o WHERE o.id = $1""", observer_id, capture_id)
        if row is not None and not row["moderator"] and row["shown"] < settings.screening_trusted_after:
            reasons.append("new_account")
    return reasons


async def screen(conn, capture_id: UUID) -> str | None:
    """Decide an unscreened upload from what is stored for it. Returns the new
    state, or None when there was nothing to decide (already screened, or
    screening is off). Safe to call again: only `pending` is ever decided.

    With screening off nothing new is `pending`; an upload left pending from
    before it was turned off reaches the queue as "not screened"."""
    if not settings.screening_enabled or capture_id is None:
        return None
    async with conn.transaction():
        row = await conn.fetchrow(
            "SELECT screening, observer_id FROM captures WHERE id = $1 FOR UPDATE", capture_id)
        if row is None or row["screening"] != "pending":
            return None
        reasons = await reasons_for(conn, capture_id, row["observer_id"])
        state = "held" if reasons else "clear"
        from app import model_screening
        if not reasons and model_screening.screening_by_model():
            # Nothing the detector can see; the local model looks too, for
            # at most `screening_model_wait_s` (app/model_screening.py).
            state = "checking"
            await model_screening.enqueue(conn, capture_id)
        await conn.execute(
            "UPDATE captures SET screening = $2, hold_reasons = $3, screened_at = now(), updated_at = now() "
            "WHERE id = $1", capture_id, state, reasons)
        if reasons:
            # In the same transaction: an alert for a hold that rolled back
            # would send a moderator to nothing.
            await notify_moderators_held(conn, capture_id, reasons)
    logger.info("capture=%s screened %s %s", capture_id, state, ",".join(reasons))
    return state


async def screen_sighting(pool, sighting_id: UUID) -> None:
    """`screen` for a /sighting upload, from a background task. Never raises:
    an upload left unscreened stays hidden and reaches the queue when stale."""
    try:
        async with pool.acquire() as conn:
            capture_id = await conn.fetchval("SELECT capture_id FROM sightings WHERE id = $1", sighting_id)
            if capture_id is not None:
                await screen(conn, capture_id)
    except Exception:
        logger.warning("screening failed for sighting=%s; it stays hidden", sighting_id, exc_info=True)


# Screening reasons a moderator should hear about at once rather than in a
# digest. What YOLO flags is "have a look", not an emergency; sexual content
# the model sees is.
URGENT_REASONS: frozenset[str] = frozenset({"model_sexual"})


async def notify_moderators_held(conn, capture_id: UUID, reasons: list[str]) -> None:
    """THE HOOK for moderator alerts (#191). An upload has just been held.

    Called inside the transaction that holds it. Log-only until #191's
    `alert_moderators` is on this branch; then this becomes
    `alert_moderators(conn, event_key=f"held_upload:{capture_id}",
    category="held_upload", subject_id=capture_id, urgent=...)`.
    """
    urgent = bool(URGENT_REASONS.intersection(reasons))
    logger.info("moderators: upload capture=%s held for review (%s)%s", capture_id,
                ",".join(reasons), " URGENT" if urgent else "")


async def record_moderator_action(conn, *, moderator_id: UUID, action: str, capture_id: UUID) -> None:
    """THE HOOK for the moderator audit log (#191), for a ruling on a held
    upload. Called inside the ruling's transaction. Log-only until #191's
    audit-log function is on this branch; then it calls that."""
    logger.info("moderator=%s %s capture=%s", moderator_id, action, capture_id)


# --- the moderator queue ---------------------------------------------------------


@router.get("/moderation/held")
async def held_queue(
    _mod: UUID = Depends(require_moderator),
    conn=Depends(get_conn),
    storage: S3Storage = Depends(get_storage),
):
    """Uploads waiting on a moderator, oldest first: held by screening, or
    never screened at all after `screening_stale_s`.

    Whole frames (`FRAME_KEYS`). The typed note and name come too, as they
    are public with the upload once it is approved.
    """
    rows = await conn.fetch(
        f"""
        SELECT c.id, c.created_at, c.screening, c.hold_reasons, c.kind,
               c.attrs->>'note' AS note,
               (SELECT array_agg(DISTINCT s.attrs->>'known_name') FILTER (WHERE s.attrs->>'known_name' IS NOT NULL)
                FROM sightings s WHERE s.capture_id = c.id) AS known_names,
               c.observer_id, o.display_name AS observer,
               {FRAME_KEYS.format(capture="c.id", limit="$2")} AS keys
        FROM captures c
        LEFT JOIN observers o ON o.id = c.observer_id
        WHERE (c.screening = 'held'
               OR (c.screening = 'pending' AND c.created_at < now() - $3 * interval '1 second'))
          -- Going with its account, or banned (#179, #180): nothing to rule on.
          AND (o.id IS NULL OR (o.deletion_requested_at IS NULL AND o.deleted_at IS NULL AND o.banned_at IS NULL))
        ORDER BY c.created_at, c.id
        LIMIT $1
        """,
        MAX_QUEUE, MAX_PHOTOS, settings.screening_stale_s,
    )
    keys = [k for r in rows for k in r["keys"]]
    urls = iter(await storage.urls([thumb_key(k) for k in keys]))
    return {"items": [
        {
            "capture_id": str(r["id"]),
            "created_at": r["created_at"],
            "kind": r["kind"],
            "reasons": list(r["hold_reasons"]) if r["screening"] == "held" else ["not_screened"],
            "observer": r["observer"],
            # Moderators only: what a ban acts on (#180).
            "observer_id": str(r["observer_id"]) if r["observer_id"] else None,
            # User-written, shown to a moderator. React escapes on render.
            "note": r["note"],
            "known_names": list(r["known_names"] or []),
            "thumb_urls": [next(urls) for _ in r["keys"]],
        }
        for r in rows
    ]}


class ScreeningVerdict(BaseModel):
    verdict: Literal["approve", "reject"]


@router.post("/moderation/capture/{capture_id}/screening")
async def rule_on_upload(
    capture_id: UUID,
    body: ScreeningVerdict,
    moderator_id: UUID = Depends(require_moderator),
    conn=Depends(get_conn),
):
    """Let a held upload through, or keep it off. Either can be ruled again.

    Only for an upload screening stopped (or never finished): one that
    screened clear is a matter for reports, not for this.
    """
    state = "approved" if body.verdict == "approve" else "rejected"
    async with conn.transaction():
        updated = await conn.fetchval(
            """UPDATE captures SET screening = $2, screening_reviewed_at = now(), screening_reviewed_by = $3,
                   updated_at = now()
               WHERE id = $1 AND screening IN ('pending','checking','held','approved','rejected') RETURNING id""",
            capture_id, state, moderator_id)
        if updated is None:
            raise HTTPException(status_code=404, detail="no held upload")
        await record_moderator_action(conn, moderator_id=moderator_id, action=f"upload_{state}",
                                      capture_id=capture_id)
    return {"status": "ok", "screening": state}
