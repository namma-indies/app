"""Reporting a sighting, and deciding what happens to it.

WHY THIS EXISTS
---------------
`sightings.review_status` has carried `pending`/`valid`/`rejected` since the
first migration, and `/map` has filtered on it for as long as it has existed.
Nothing ever wrote anything but `valid`. The filter was unreachable code: no
path in the app, the API or any script could hide a sighting, so a photo that
should not be on a shared map stayed on it, permanently, unless someone opened
psql.

Apple's Guideline 1.2 requires a report mechanism and a way to act on it, and
Google Play's UGC policy requires reporting. Both are true, and neither is the
reason this is urgent. The map shows where free-roaming dogs are; if a sighting
puts an animal at risk, "file an issue on GitHub" is not a takedown path.

THE PRODUCT DECISION IN HERE
----------------------------
**Two** people must report a sighting before it hides, pending a moderator.

This started at one and was changed deliberately. Hiding on the first report
makes the harm from a sighting that endangers a dog stop immediately, which is
the argument for it -- but it also means any single account can take any photo
off the shared map on its own say-so, and at pilot scale the people logging
sightings are the people whose work disappears. Requiring a second, independent
voice costs a delay measured in however long it takes one more person to agree,
and buys the property that no one person can blank the map alone.

It is two *distinct* reporters, not two taps: the primary key on
(sighting_id, reporter_id) means one account reporting twice is one report.

A report is always recorded and always reaches the queue, whether or not it
crossed the threshold. Hiding is what waits for the second voice; a moderator
seeing it does not.

Change the number with `HIDE_AT_REPORTS`. The rest of the file does not care --
set it to 1 to restore hide-on-first-report.

WHO MODERATES
-------------
`observers.trust_tier = 'moderator'`. That column has been on the table since
0001 and nothing has ever read or written it; the name already meant this, so
this adds no schema. Set it by hand, deliberately:

    UPDATE observers SET trust_tier = 'moderator' WHERE email = '...';

Deliberately not self-serve and deliberately not inferred from anything else. A
moderator can unhide content, which is a decision about someone else's safety
report, and that should be a thing a person was given rather than a thing they
accumulated.
"""

import logging
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException

from app.auth.deps import require_moderator, require_observer
from app.config import settings
from app.deps import get_conn, get_storage
from app.detect_reid import DETECTOR_NAME
from app.photos import thumb_key
from app.storage.s3 import S3Storage

logger = logging.getLogger(__name__)

router = APIRouter()

# See the module docstring. Distinct reporters needed before a sighting hides;
# a moderator restores. 1 restores the original hide-on-first-report behaviour.
HIDE_AT_REPORTS = 2

# Capped because it is stored and later rendered to a moderator. Long enough
# for a sentence explaining what is wrong, short enough not to be a channel.
MAX_NOTE = 500

MAX_QUEUE = 200

Reason = Literal["endangers_dog", "not_a_dog", "wrong_place", "offensive", "other"]



@router.get("/me")
async def me(observer_id: UUID = Depends(require_observer), conn=Depends(get_conn)):
    """Who the session belongs to, and whether they moderate.

    The client needs the second one to decide whether to render the moderation
    tab at all. It is a display hint and nothing more -- every moderation
    endpoint checks the tier itself, because a client-side flag is a suggestion.
    """
    row = await conn.fetchrow(
        "SELECT display_name, trust_tier FROM observers WHERE id = $1", observer_id
    )
    if row is None:
        raise HTTPException(status_code=401)
    return {
        "id": str(observer_id),
        "display_name": row["display_name"],
        "is_moderator": row["trust_tier"] == "moderator",
        "multi_animal_enabled": settings.multi_animal_enabled and settings.media_jobs_enabled,
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

    exists = await conn.fetchval("SELECT 1 FROM sightings WHERE id = $1", sighting_id)
    if not exists:
        raise HTTPException(status_code=404, detail="no such sighting")

    async with conn.transaction():
        capture_id = await conn.fetchval("SELECT capture_id FROM sightings WHERE id=$1", sighting_id)
        await conn.execute("SELECT id FROM captures WHERE id=$1 FOR UPDATE", capture_id)
        await conn.execute(
            """
            INSERT INTO sighting_reports (sighting_id, reporter_id, reason, note)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (sighting_id, reporter_id) DO UPDATE
                SET reason = EXCLUDED.reason,
                    note = EXCLUDED.note,
                    created_at = now()
            """,
            sighting_id,
            observer_id,
            reason,
            (note or "").strip() or None,
        )
        # `reviewed_at IS NULL` is what makes a moderator's decision stick.
        # Hiding on `review_status = 'valid'` alone looks equivalent and is
        # not: once a moderator has looked and restored a sighting, the next
        # person to tap report takes it straight back down, so the human
        # decision is advisory and the last tap wins. The report is still
        # recorded, and the queue surfaces anything reported *since* the
        # last review, so re-reporting reaches a human -- it just does not
        # reach past one.
        #
        # The count is of rows in sighting_reports, which is one per reporter
        # by primary key, so this is "two people agree" and not "two taps".
        await conn.execute(
            "UPDATE sightings SET review_status = 'pending', updated_at = now() "
            "WHERE (id = $1 OR capture_id=(SELECT capture_id FROM sightings WHERE id=$1)) "
            "AND review_status = 'valid' AND reviewed_at IS NULL "
            "AND (SELECT count(DISTINCT r.reporter_id) FROM sighting_reports r JOIN sightings src ON src.id=r.sighting_id "
            "WHERE src.id=$1 OR src.capture_id=(SELECT capture_id FROM sightings WHERE id=$1)) >= $2",
            sighting_id,
            HIDE_AT_REPORTS,
        )

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
        """
        SELECT s.id,
               s.captured_at,
               s.review_status,
               o.display_name AS observer,
               count(r.*) AS report_count,
               max(r.created_at) AS last_reported,
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
        WHERE s.review_status <> 'rejected'
        GROUP BY s.id, s.captured_at, s.review_status, s.reviewed_at,
                 o.display_name, p.s3_key
        -- Still open, or reported again since the last ruling. Without the
        -- second half a moderator who restores something watches it sit in
        -- their queue forever; without the first, a fresh concern about an
        -- already-reviewed sighting never reaches anyone.
        HAVING s.reviewed_at IS NULL OR max(r.created_at) > s.reviewed_at
        ORDER BY count(r.*) DESC, max(r.created_at) DESC
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
                "observer": r["observer"],
                "report_count": r["report_count"],
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
    async with conn.transaction():
        capture_id = await conn.fetchval("SELECT capture_id FROM sightings WHERE id=$1", sighting_id)
        await conn.execute("SELECT id FROM captures WHERE id=$1 FOR UPDATE", capture_id)
        updated = await conn.fetchval(
            "UPDATE sightings SET review_status = $2, reviewed_at = now(), "
            "reviewed_by = $3, updated_at = now() WHERE id = $1 OR capture_id=$4 RETURNING id",
            sighting_id, verdict, moderator_id, capture_id,
        )
    if updated is None:
        raise HTTPException(status_code=404, detail="no such sighting")
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
        """
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
    updated = await conn.fetchval(
        "UPDATE sightings SET animal_override = $2, animal_reviewed_at = now(), "
        "animal_reviewed_by = $3, updated_at = now() WHERE id = $1 RETURNING id",
        sighting_id,
        override,
        moderator_id,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="no such sighting")
    logger.info(
        "sighting=%s ruled %s by moderator=%s", sighting_id, verdict, moderator_id
    )
    return {"status": "ok", "animal_override": override}
