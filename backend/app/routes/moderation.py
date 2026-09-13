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
from app.deps import get_conn, get_storage
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
            "WHERE id = $1 AND review_status = 'valid' AND reviewed_at IS NULL "
            "  AND (SELECT count(*) FROM sighting_reports WHERE sighting_id = $1) >= $2",
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
    updated = await conn.fetchval(
        "UPDATE sightings SET review_status = $2, reviewed_at = now(), "
        "reviewed_by = $3, updated_at = now() WHERE id = $1 RETURNING id",
        sighting_id,
        verdict,
        moderator_id,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="no such sighting")
    logger.info(
        "sighting=%s reviewed as %s by moderator=%s", sighting_id, verdict, moderator_id
    )
    return {"status": "ok", "review_status": verdict}
