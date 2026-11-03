"""Reports and reasoned moderator decisions.

Ordinary reports need two distinct reporters before shared surfaces hide a
capture. People/privacy, sexual-content and illegal-content reports hold it
on the first report and alert immediately (#198). A fresh severe concern can
hold a previously approved capture again; ordinary reports cannot overturn a
ruling. All open concerns reach the queue, with an original response clock.

Moderators are deliberately assigned through observers.trust_tier. Decisions
require a reason and append an audit entry in the same transaction. No action
here deletes evidence. Naming moderators and a rota remains in #191.
"""

import logging
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException
from pydantic import BaseModel, Field

from app.aggregates import owner_active
from app.auth.deps import require_moderator, require_observer
from app.config import settings
from app.deps import get_conn, get_storage
from app.detect_reid import DETECTOR_NAME
from app.ids import uuid7
from app.moderation_ops import SEVERE_REASONS, action_reason, record_action, report_clock
from app.moderator_alerts import alert_moderators, recipients
from app.photos import thumb_key
from app.security import email_hash
from app.storage.s3 import S3Storage
from app.transcription_jobs import transcription_enabled

logger = logging.getLogger(__name__)

router = APIRouter()

# See the module docstring. Distinct reporters needed before a sighting hides;
# a moderator restores. 1 restores the original hide-on-first-report behaviour.
HIDE_AT_REPORTS = 2

# Capped because it is stored and later rendered to a moderator. Long enough
# for a sentence explaining what is wrong, short enough not to be a channel.
MAX_NOTE = 500

MAX_QUEUE = 200

Reason = Literal["endangers_dog", "not_a_dog", "wrong_place", "offensive", "other",
                 "people_privacy", "sexual_content", "illegal_content"]



@router.get("/me")
async def me(observer_id: UUID = Depends(require_observer), conn=Depends(get_conn)):
    """Who the session belongs to, and whether they moderate.

    The client needs the second one to decide whether to render the moderation
    tab at all. It is a display hint and nothing more -- every moderation
    endpoint checks the tier itself, because a client-side flag is a suggestion.
    """
    row = await conn.fetchrow(
        "SELECT display_name, trust_tier, social_consent, x_handle, note_language FROM observers WHERE id = $1",
        observer_id,
    )
    if row is None:
        raise HTTPException(status_code=401)
    return {
        "id": str(observer_id),
        "display_name": row["display_name"],
        "is_moderator": row["trust_tier"] == "moderator",
        # Whether to offer "share with the Namma Indies team" at all: only to
        # someone who has agreed to be posted and has a handle to be credited.
        "can_share_socially": bool(row["social_consent"] and row["x_handle"]),
        "multi_animal_enabled": settings.multi_animal_enabled and settings.media_jobs_enabled,
        # Whether "Add voice note" can work now (#156): only with a worker
        # transcribing, and how many a sighting can take.
        "later_voice_notes_enabled": transcription_enabled(),
        "later_voice_notes_max": settings.later_voice_notes_max,
        # The language they speak their notes in (#167); null until asked and answered.
        "note_language": row["note_language"],
    }


@router.post("/sighting/{sighting_id}/report")
async def report_sighting(
    sighting_id: UUID,
    reason: Reason = Form(...),
    note: str | None = Form(None),
    observer_id: UUID = Depends(require_observer),
    conn=Depends(get_conn),
):
    """Flag a sighting for review, and take it off the shared surfaces now.

    Idempotent: the primary key on (sighting_id, reporter_id) means a double
    tap on a slow connection cannot inflate a count a moderator will read as
    "several people are worried about this".
    """
    if note is not None and len(note) > MAX_NOTE:
        raise HTTPException(status_code=422, detail=f"note is longer than {MAX_NOTE}")

    note = (note or "").strip() or None
    async with conn.transaction():
        capture_id = await conn.fetchval("SELECT capture_id FROM sightings WHERE id=$1", sighting_id)
        await conn.execute("SELECT id FROM captures WHERE id=$1 FOR UPDATE", capture_id)
        sighting = await conn.fetchrow("SELECT review_status FROM sightings WHERE id=$1", sighting_id)
        if sighting is None:
            raise HTTPException(status_code=404, detail="no such sighting")
        previous = await conn.fetchrow(
            "SELECT reason, note, resolved_at, severe_at FROM sighting_reports WHERE sighting_id=$1 AND reporter_id=$2 FOR UPDATE",
            sighting_id, observer_id)
        # A retry is not a new concern and must not reset the response clock
        # or send another urgent email. A report after a ruling reopens it.
        changed = previous is None or previous["resolved_at"] is not None or (previous["reason"], previous["note"]) != (reason, note)
        if not changed:
            return {"status": "ok", "hidden": sighting["review_status"] != "valid"}
        await conn.execute(
            """
            INSERT INTO sighting_reports (sighting_id, reporter_id, reason, note, severe_at)
            VALUES ($1, $2, $3, $4, CASE WHEN $5 THEN now() END)
            ON CONFLICT (sighting_id, reporter_id) DO UPDATE
                SET reason = EXCLUDED.reason,
                    note = EXCLUDED.note,
                    created_at = now(),
                    opened_at = CASE WHEN sighting_reports.resolved_at IS NOT NULL
                                     THEN now() ELSE sighting_reports.opened_at END,
                    severe_at = CASE WHEN sighting_reports.resolved_at IS NOT NULL THEN EXCLUDED.severe_at
                                     ELSE COALESCE(sighting_reports.severe_at, EXCLUDED.severe_at) END,
                    resolved_at = NULL
            """,
            sighting_id,
            observer_id,
            reason,
            note,
            reason in SEVERE_REASONS,
        )
        # Ordinary reports keep a human ruling sticky. A new severe concern
        # takes precedence over an earlier approval until reviewed again. The
        # whole capture is held, since sibling animals share the photograph.
        await conn.execute(
            "UPDATE sightings SET review_status = 'pending', updated_at = now() "
            "WHERE (id = $1 OR capture_id=(SELECT capture_id FROM sightings WHERE id=$1)) "
            "AND review_status <> 'rejected' AND ((reviewed_at IS NULL "
            "AND (SELECT count(DISTINCT r.reporter_id) FROM sighting_reports r JOIN sightings src ON src.id=r.sighting_id "
            "WHERE (src.id=$1 OR src.capture_id=(SELECT capture_id FROM sightings WHERE id=$1)) "
            "AND r.resolved_at IS NULL) >= $2) OR $3)",
            sighting_id,
            HIDE_AT_REPORTS,
            reason in SEVERE_REASONS,
        )
        # Rejecting is already a stronger hold. Fresh severe concerns about a
        # previously approved item still hide it until the next human ruling.
        notify = (previous is None or previous["resolved_at"] is not None
                  or (reason in SEVERE_REASONS and previous["severe_at"] is None))
        if notify:
            await alert_moderators(conn, event_key=f"report:{uuid7()}", category=f"report_{reason}",
                                   subject_id=sighting_id, urgent=reason in SEVERE_REASONS)

    logger.info(
        "sighting=%s reported by observer=%s reason=%s", sighting_id, observer_id, reason
    )
    # Read back rather than assume. The report may have crossed the threshold,
    # may not have, or the sighting may already have been hidden by an earlier
    # pair -- and "did my report take it down" is the one thing the reporter
    # actually wants to know.
    hidden = await conn.fetchval(
        "SELECT review_status <> 'valid' FROM sightings WHERE id = $1", sighting_id
    )
    return {"status": "ok", "hidden": bool(hidden)}


@router.get("/moderation/queue")
async def moderation_queue(
    _mod: UUID = Depends(require_moderator),
    conn=Depends(get_conn),
    storage: S3Storage = Depends(get_storage),
):
    """Everything waiting on a human, worst-reported first.

    Includes what was said and by whom. A moderator deciding whether a photo
    endangers an animal needs the reason; the same photo can be fine or not
    depending on what someone recognised in it.
    """
    rows = await conn.fetch(
        f"""
        SELECT s.id,
               s.captured_at,
               s.review_status,
               s.reviewed_at,
               s.species,
               o.display_name AS observer,
               s.observer_id,
               count(r.*) AS report_count,
               max(r.created_at) AS last_reported,
               min(r.opened_at) AS first_reported,
               min(r.severe_at) AS first_severe,
               array_agg(r.reason ORDER BY r.created_at DESC) AS reasons,
               array_remove(array_agg(r.note ORDER BY r.created_at DESC), NULL) AS notes,
               p.s3_key
        FROM sightings s
        JOIN sighting_reports r ON r.sighting_id = s.id
        LEFT JOIN observers o ON o.id = s.observer_id
        -- One representative photo, in a lateral rather than a join: a clip
        -- yields up to twelve frames and would otherwise multiply the row.
        LEFT JOIN LATERAL (
            SELECT s3_key FROM photos WHERE sighting_id = s.id
            ORDER BY created_at, id LIMIT 1
        ) p ON TRUE
        WHERE r.resolved_at IS NULL
          -- Going with its account (#179): nothing left to rule on.
          AND {owner_active('s')}
        GROUP BY s.id, s.captured_at, s.review_status, s.reviewed_at, s.species,
                 o.display_name, p.s3_key
        -- A ruling resolves the capture's open report rows atomically.
        -- A later report reopens its row, with a fresh response clock.
        ORDER BY (min(r.severe_at) IS NOT NULL) DESC,
                 count(r.*) DESC, min(r.opened_at), s.id
        LIMIT $1
        """,
        MAX_QUEUE,
    )
    keys = [thumb_key(r["s3_key"]) for r in rows if r["s3_key"]]
    urls = await storage.urls(keys)
    thumbs = iter(urls)
    return {
        "items": [
            {
                "sighting_id": str(r["id"]),
                "captured_at": r["captured_at"],
                "review_status": r["review_status"],
                "reported_after_review": r["reviewed_at"] is not None,
                "species": r["species"],
                "observer": r["observer"],
                # Moderators only: what a ban acts on (#180).
                "observer_id": str(r["observer_id"]) if r["observer_id"] else None,
                "report_count": r["report_count"],
                "last_reported_at": r["last_reported"],
                **report_clock(r["first_reported"], r["first_severe"]),
                "reasons": list(r["reasons"] or []),
                # User-written, shown to a moderator. React escapes on render.
                "notes": list(r["notes"] or []),
                "thumb_url": next(thumbs) if r["s3_key"] else None,
            }
            for r in rows
        ]
    }


@router.post("/sighting/{sighting_id}/review")
async def review_sighting(
    sighting_id: UUID,
    verdict: Literal["valid", "rejected"] = Form(...),
    reason: str = Form(..., min_length=1, max_length=MAX_NOTE),
    action: Literal["hide", "unhide", "approve", "reject", "dismiss"] | None = Form(None),
    moderator_id: UUID = Depends(require_moderator),
    conn=Depends(get_conn),
):
    """A moderator's decision: put it back, or keep it down.

    `rejected` hides the sighting from `/map`, `/dogs` and `/proposals`, and
    stops it seeding new identities in candidate search. It does not delete
    anything. The photograph is evidence of something that happened, the S3
    objects stay, and the row keeps its reports -- deletion is not reversible
    and this decision should be.
    """
    reason = action_reason(reason)
    if action is not None and (action in {"hide", "reject"}) != (verdict == "rejected"):
        raise HTTPException(422, "Action and verdict disagree")
    async with conn.transaction():
        capture_id = await conn.fetchval("SELECT capture_id FROM sightings WHERE id=$1", sighting_id)
        await conn.execute("SELECT id FROM captures WHERE id=$1 FOR UPDATE", capture_id)
        old_rows = await conn.fetch("SELECT id, review_status FROM sightings WHERE id=$1 OR capture_id=$2 FOR UPDATE", sighting_id, capture_id)
        old_states = {r["id"]: r["review_status"] for r in old_rows}
        if sighting_id not in old_states:
            raise HTTPException(status_code=404, detail="no such sighting")
        updated = await conn.fetch(
            "UPDATE sightings SET review_status = $2, reviewed_at = now(), "
            "reviewed_by = $3, updated_at = now() WHERE id = $1 OR capture_id=$4 RETURNING id",
            sighting_id, verdict, moderator_id, capture_id,
        )
        await conn.execute(
            "UPDATE sighting_reports SET resolved_at=now() WHERE sighting_id=ANY($1::uuid[]) AND resolved_at IS NULL",
            [r["id"] for r in updated])
        for r in updated:
            old_state = old_states[r["id"]]
            audit_action = action or ("hide" if verdict == "rejected" else "unhide" if old_state != "valid" else "dismiss")
            await record_action(conn, moderator_id=moderator_id, target_kind="sighting", target_id=r["id"],
                                action=audit_action, reason=reason, previous_state=old_state, new_state=verdict)
    logger.info(
        "sighting=%s reviewed as %s by moderator=%s", sighting_id, verdict, moderator_id
    )
    return {"status": "ok", "review_status": verdict}


@router.get("/moderation/animals")
async def animal_queue(
    _mod: UUID = Depends(require_moderator),
    conn=Depends(get_conn),
    storage: S3Storage = Depends(get_storage),
):
    """Photos the detector thinks have no animal in them, least likely first.

    THIS IS ALSO HOW THE THRESHOLD GETS CHOSEN
    ------------------------------------------
    A histogram of scores says where they cluster; it cannot say where the
    detector starts being wrong. Walking this list from the bottom does: you
    stop being able to say "no animal" at some point, and that point is
    `animal_confidence_min`. So the review pass that checks the cleanup is the
    same pass that produces the number, and it runs while the filter is still
    inert and nothing has been hidden from anyone.

    Dog and cat are reported separately rather than as the max the sighting
    was scored with. The product call is that any animal counts, but the
    question a moderator is actually answering is "is there a dog in this",
    and 0.82 alone cannot distinguish a dog from a cat.

    Not folded into `/moderation/queue`: that one aggregates reports with a
    HAVING clause, and a second source would make both unreadable.

    THE CEILING, AND WHY IT IS NOT THE OTHER NUMBER
    -----------------------------------------------
    Only sightings scoring below `animal_review_max` appear. Without it the
    queue is every scored sighting the moment the rescore finishes -- the 0.95
    dogs sitting behind the 0.02 sofas -- and a surface that calls ordinary
    content "flagged" teaches a moderator to stop reading it.

    This is a second number about the same column, which is precisely the
    confusion #67 was about, so the difference is worth stating plainly:
    `animal_confidence_min` decides what the WORLD sees and is still 0.0;
    `animal_review_max` decides only what a MODERATOR is asked to look at.
    Nothing outside this endpoint reads it, moving it hides nothing from
    anyone, and a ruling already made is unaffected -- `animal_override` is
    stored, not recomputed.
    """
    rows = await conn.fetch(
        f"""
        SELECT s.id,
               s.captured_at,
               s.animal_confidence,
               o.display_name AS observer,
               pd.dog, pd.cat, pd.s3_key
        FROM sightings s
        LEFT JOIN observers o ON o.id = s.observer_id
        -- One representative photo and its scores, in a lateral rather than a
        -- join: a clip yields up to twelve frames and would multiply the row.
        -- The photo shown is the one that scored highest, so a moderator
        -- rules against the sighting's best evidence rather than its worst.
        LEFT JOIN LATERAL (
            SELECT ph.s3_key, dd.dog, dd.cat
            FROM photos ph
            JOIN detections dd ON dd.photo_id = ph.id AND dd.model = $1
            WHERE ph.sighting_id = s.id
            ORDER BY greatest(dd.dog, dd.cat) DESC
            LIMIT 1
        ) pd ON TRUE
        WHERE s.animal_confidence IS NOT NULL
          AND s.animal_override IS NULL
          AND {owner_active('s')}
          -- The ceiling. `::real` for the same reason the map predicate casts:
          -- `animal_confidence` is `real`, and comparing it against an
          -- unsuffixed literal widens both to double precision, where a float4
          -- does not land on the decimal you typed. Casting keeps the
          -- comparison in the column's own type, so the boundary behaves.
          AND s.animal_confidence < $3::real
        -- s.id tiebreaks. Two sightings sharing a score and a captured_at
        -- would otherwise leave their relative order to the plan, so the
        -- queue could reshuffle between one fetch and the next.
        ORDER BY s.animal_confidence ASC, s.captured_at DESC, s.id
        LIMIT $2
        """,
        DETECTOR_NAME,
        MAX_QUEUE,
        settings.animal_review_max,
    )
    keys = [thumb_key(r["s3_key"]) for r in rows if r["s3_key"]]
    urls = await storage.urls(keys)
    thumbs = iter(urls)
    return {
        "items": [
            {
                "sighting_id": str(r["id"]),
                "captured_at": r["captured_at"],
                "observer": r["observer"],
                "animal_confidence": r["animal_confidence"],
                "dog": r["dog"],
                "cat": r["cat"],
                "thumb_url": next(thumbs) if r["s3_key"] else None,
            }
            for r in rows
        ]
    }


@router.post("/sighting/{sighting_id}/animal")
async def rule_on_animal(
    sighting_id: UUID,
    verdict: Literal["animal", "no_animal"] = Form(...),
    reason: str = Form(..., min_length=1, max_length=MAX_NOTE),
    moderator_id: UUID = Depends(require_moderator),
    conn=Depends(get_conn),
):
    """A person's answer to "is there an animal in this", which beats the model's.

    Written to `animal_override`, never to `review_status`. That column and
    `reviewed_at` mean "a person ruled on a report" (0010), and a verdict about
    what is in the frame is a different question -- the same photo can carry
    both answers. Keeping them apart is also what lets this ruling survive a
    threshold retune and the next detector swap, so the corpus is reviewed
    once rather than after every change.

    Reversible by ruling again, and it deletes nothing.
    """
    override = verdict == "animal"
    reason = action_reason(reason)
    async with conn.transaction():
        old = await conn.fetchrow("SELECT animal_override FROM sightings WHERE id=$1 FOR UPDATE", sighting_id)
        if old is None:
            raise HTTPException(status_code=404, detail="no such sighting")
        await conn.execute(
            "UPDATE sightings SET animal_override = $2, animal_reviewed_at = now(), "
            "animal_reviewed_by = $3, updated_at = now() WHERE id = $1", sighting_id, override, moderator_id)
        await record_action(conn, moderator_id=moderator_id, target_kind="sighting", target_id=sighting_id,
                            action="approve" if override else "reject", reason=reason,
                            previous_state=f"animal_override={old['animal_override']}", new_state=f"animal_override={override}")
    logger.info(
        "sighting=%s ruled %s by moderator=%s", sighting_id, verdict, moderator_id
    )
    return {"status": "ok", "animal_override": override}


# --- bans (#180) ------------------------------------------------------------------
#
# A moderator's ban hides everything someone logged from everyone, ends their
# sessions and refuses their address at sign-in. Unlike a block, which only
# one person's view, it is for everyone, and so it is a person's decision:
# nothing bans automatically, however many people have blocked someone.
# Reversible. A banned person can still delete their account (/account), and
# the ban outlives that as a keyed hash of their address (banned_emails).


class BanBody(BaseModel):
    reason: str = Field(..., min_length=1, max_length=MAX_NOTE)


@router.post("/moderation/observer/{observer_id}/ban")
async def ban(observer_id: UUID, body: BanBody, moderator_id: UUID = Depends(require_moderator),
              conn=Depends(get_conn)):
    reason = action_reason(body.reason)
    async with conn.transaction():
        target = await conn.fetchrow(
            "SELECT email, trust_tier, banned_at FROM observers WHERE id=$1 AND deleted_at IS NULL FOR UPDATE",
            observer_id)
        if target is None:
            raise HTTPException(status_code=404, detail="no such observer")
        if observer_id == moderator_id or target["trust_tier"] == "moderator":
            # Moderators are made by hand, and unmade the same way.
            raise HTTPException(status_code=409, detail="a moderator cannot be banned here")
        await conn.execute(
            "UPDATE observers SET banned_at = COALESCE(banned_at, now()), banned_by = $2, ban_reason = $3, "
            "updated_at = now() WHERE id = $1",
            observer_id, moderator_id, reason)
        if target["email"]:
            await conn.execute("INSERT INTO banned_emails (email_hmac) VALUES ($1) ON CONFLICT DO NOTHING",
                               email_hash(target["email"]))
        await record_action(conn, moderator_id=moderator_id, target_kind="observer", target_id=observer_id,
                            action="ban", reason=reason, previous_state="banned" if target["banned_at"] else "active",
                            new_state="banned")
    logger.info("observer=%s banned by moderator=%s", observer_id, moderator_id)
    return {"status": "banned"}


@router.post("/moderation/observer/{observer_id}/unban")
async def unban(observer_id: UUID, body: BanBody, moderator_id: UUID = Depends(require_moderator), conn=Depends(get_conn)):
    reason = action_reason(body.reason)
    async with conn.transaction():
        email = await conn.fetchval(
            "UPDATE observers SET banned_at = NULL, banned_by = NULL, ban_reason = NULL, updated_at = now() "
            "WHERE id = $1 AND banned_at IS NOT NULL AND deleted_at IS NULL RETURNING coalesce(email, '')",
            observer_id)
        if email is None:
            raise HTTPException(status_code=404, detail="no such ban")
        if email:
            await conn.execute("DELETE FROM banned_emails WHERE email_hmac = $1", email_hash(email))
        await record_action(conn, moderator_id=moderator_id, target_kind="observer", target_id=observer_id,
                            action="unban", reason=reason, previous_state="banned", new_state="active")
    logger.info("observer=%s unbanned by moderator=%s", observer_id, moderator_id)
    return {"status": "active"}


@router.get("/moderation/audit")
async def audit_log(before: UUID | None = None, _mod: UUID = Depends(require_moderator), conn=Depends(get_conn)):
    rows = await conn.fetch(
        "SELECT a.*, o.display_name AS moderator FROM moderation_actions a "
        "LEFT JOIN observers o ON o.id=a.moderator_id "
        "WHERE ($1::uuid IS NULL OR (a.created_at,a.id) < "
        "(SELECT created_at,id FROM moderation_actions WHERE id=$1)) "
        "ORDER BY a.created_at DESC,a.id DESC LIMIT 100", before)
    return {"items": [dict(r) for r in rows], "next_cursor": str(rows[-1]["id"]) if len(rows) == 100 else None}


@router.get("/moderation/alerts/status")
async def alerts_status(_mod: UUID = Depends(require_moderator), conn=Depends(get_conn)):
    row = await conn.fetchrow(
        "SELECT (SELECT count(*) FROM moderator_alerts WHERE queued_at IS NULL) AS pending_events, "
        "count(*) FILTER (WHERE sent_at IS NULL) AS pending_mail, "
        "count(*) FILTER (WHERE sent_at IS NULL AND attempts>0 AND last_error IS NOT NULL) AS failed_mail "
        "FROM moderation_mail")
    return {**dict(row), "recipient_count": len(recipients()), "email_sender": settings.email_sender,
            "delivery_configured": bool(recipients()) and settings.email_sender == "ses"}
