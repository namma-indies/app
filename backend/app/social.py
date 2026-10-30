"""The daily @nammaindies post: who and what may go out, and what it says.

Design: docs/specs/2026-10-24-x-daily-post-design.md.

Eligibility is deliberately one query used twice -- when a moderator picks a
photo, and again at the moment the daily run posts it. A sighting rejected, a
copy hidden, a share withdrawn or consent removed in between must stop the
post, and a second, slightly different copy of the rule is how one of those
would slip through.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from app.aggregates import coarse_geog
from app.config import settings
from app.x_client import XUncertain, prepare_image

log = logging.getLogger(__name__)

# A sighting may be posted when it is still showing, is not a hidden copy, was
# shared by a contributor who has consented and has a handle to credit, and is
# not a video -- clips have no single photo a person chose.
#
# Postable photos are the ones a person chose: hashed uploads hanging off a
# /sighting, or the hashed source photo a /capture sighting was cut from. Crops
# and clip frames carry no hash (see migration 0016), so they never qualify;
# the whole frame goes out, never the crop (design: "whole photo").
#
# The observer is the capture's when there is one, which is the same rule
# `propose_known_names` uses for credit.
_POSTABLE_PHOTOS = """
    WITH s AS (
        SELECT s.id
        FROM sightings s
        LEFT JOIN captures c ON c.id = s.capture_id
        JOIN observers o ON o.id = COALESCE(c.observer_id, s.observer_id)
        WHERE s.id = $1
          AND s.review_status = 'valid'
          AND s.duplicate_of IS NULL
          AND s.shared_with_team_at IS NOT NULL
          AND o.social_consent
          AND o.x_handle IS NOT NULL
          AND COALESCE(c.kind, 'photo') <> 'video'
          AND COALESCE(s.attrs->>'source', '') <> 'video'
    )
    SELECT id FROM (
        SELECT p.id, p.created_at FROM photos p JOIN s ON p.sighting_id = s.id
        WHERE p.content_sha256 IS NOT NULL
        UNION
        SELECT src.id, src.created_at FROM animal_instances a
        JOIN s ON a.sighting_id = s.id
        JOIN photos src ON src.id = a.source_photo_id
        WHERE src.content_sha256 IS NOT NULL
    ) chosen
    ORDER BY created_at, id
"""


_CAN_SHARE = "SELECT social_consent AND x_handle IS NOT NULL FROM observers WHERE id = $1"


async def can_share(conn, observer_id: UUID) -> bool:
    return bool(await conn.fetchval(_CAN_SHARE, observer_id))


async def mark_shared(conn, sighting_id: UUID, observer_id: UUID) -> None:
    """Offer a sighting to the team, if its observer may be posted at all.

    Silently a no-op otherwise: the option is not shown to anyone without
    consent, so a request carrying it anyway is ignored rather than stored as
    a share nobody can act on."""
    await conn.execute(
        f"UPDATE sightings SET shared_with_team_at = now() "
        f"WHERE id = $1 AND shared_with_team_at IS NULL AND ({_CAN_SHARE.replace('$1', '$2')})",
        sighting_id,
        observer_id,
    )


async def unshare(conn, sighting_id: UUID) -> None:
    """Withdraw a share. A pick still waiting in the queue goes with it; a
    post already on X stays recorded, and the queue flags it for an admin to
    take down (design: flag, never delete from X automatically)."""
    await conn.execute(
        "UPDATE sightings SET shared_with_team_at = NULL WHERE id = $1", sighting_id
    )
    await conn.execute(
        "DELETE FROM social_picks WHERE sighting_id = $1 AND status = 'queued'", sighting_id
    )


async def postable_photos(conn, sighting_id: UUID) -> list[UUID]:
    """The photos of this sighting that may be posted, oldest first; empty
    when the sighting itself may not be."""
    return [r["id"] for r in await conn.fetch(_POSTABLE_PHOTOS, sighting_id)]


async def is_postable(conn, sighting_id: UUID, photo_id: UUID) -> bool:
    return photo_id in await postable_photos(conn, sighting_id)


async def area_name(conn, sighting_id: UUID) -> str | None:
    """The ward a sighting falls in, by the same rule `/stats/areas` uses: the
    area covering its grid cell centre (never the exact point, which near a
    boundary would say which side of it the dog is), first by id, so a point
    on a shared boundary does not move between names from one request to the
    next. None when there is no location or no area covers it -- the moderator
    types the place instead, since guessing a city is how a caption ends up
    false."""
    return await conn.fetchval(
        f"""
        SELECT a.name FROM sightings s
        JOIN areas a ON a.kind = $2 AND ST_Covers(a.geog, {coarse_geog()})
        WHERE s.id = $1
        ORDER BY a.id
        LIMIT 1
        """,
        sighting_id,
        settings.social_area_kind,
    )


AREA_PLACEHOLDER = "{area}"


def default_caption(area: str | None, handle: str) -> str:
    """The caption a pick starts from. With no area the placeholder stays in,
    and `validate_caption` refuses it until a person has replaced it."""
    return f"Spotted in {area or AREA_PLACEHOLDER} 📸 @{handle}"


# X's weighted limit is 280 and counts an emoji as two; 260 plain characters
# stays safely under it without reimplementing X's counter.
MAX_CAPTION = 260

# A post with a link costs ~13x one without (X pay-per-use: $0.20 vs $0.015),
# and X turns a bare "domain.tld" into a link on its own, on far more endings
# than any short list (.dog, .pet, .city...). So anything shaped like
# name.letters is refused. The price is a false positive like "St.Marks",
# which the message tells the moderator how to fix: a space after the dot.
_LINK = re.compile(r"https?://|www\.|\b[\w-]+\.[a-z]{2,}\b", re.IGNORECASE)


def validate_caption(text: str) -> str | None:
    """Why this caption can't be posted, or None when it can."""
    if not text or not text.strip():
        return "Write a caption."
    if AREA_PLACEHOLDER in text:
        return "Replace {area} with where they were seen."
    if _LINK.search(text):
        return "Captions can't contain links. If it isn't one, put a space after the full stop."
    if len(text) > MAX_CAPTION:
        return f"Keep the caption to {MAX_CAPTION} characters."
    return None


# ---------------------------------------------------------------------------
# The daily run
# ---------------------------------------------------------------------------

# Picks never attempted go first, oldest pick first; a pick X refused goes
# behind them, so one that X keeps refusing (a caption X counts as too long,
# duplicate text) cannot stop every post queued after it. Picks that have all
# been tried rotate by oldest attempt. The POSTS screen sorts the same way.
QUEUE_ORDER = "last_attempt_at NULLS FIRST, picked_at, id"

# Distinct from matching's lock (734821902): the two never need each other.
_DAILY_LOCK = 734821903
NOT_ELIGIBLE = "no longer eligible"


@dataclass
class RunResult:
    # 'posted' | 'dry_run' | 'already_posted_today' | 'empty'
    # | 'blocked_by_uncertain' | 'failed'
    outcome: str
    pick_id: UUID | None = None
    x_post_id: str | None = None
    detail: str | None = None


async def run_daily(pool, storage, client, *, today: date, enabled: bool) -> RunResult:
    """Post the oldest eligible pick, if nothing has gone out today.

    The order of the checks is the design's anti-goals in code:

    1. Something already posted today -> stop. The timer is `Persistent`, so
       a reboot can fire it twice; `posted_on` is also unique in the schema.
    2. A row left `posting` -> stop. X may have that post; until a person has
       checked the account, posting anything risks the same dog twice.
    3. Walk the queue oldest first, re-checking eligibility now, not at pick
       time -- moderation, a share withdrawn or consent removed in between
       must stop the post. Skipped rows stay queued with a reason, so the
       queue shows why they did not go.
    4. Disabled -> log what would have gone out and change nothing.

    The `posting` mark is committed before X is called, so the decision to
    post survives a crash in the middle of posting.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", _DAILY_LOCK)
            if await conn.fetchval("SELECT 1 FROM social_picks WHERE posted_on = $1", today):
                return RunResult("already_posted_today")
            stuck = await conn.fetchval(
                "SELECT id FROM social_picks WHERE status = 'posting' ORDER BY picked_at LIMIT 1")
            if stuck:
                return RunResult("blocked_by_uncertain", pick_id=stuck,
                                 detail="a post was started and never confirmed; check X")
            head = None
            for row in await conn.fetch(
                    "SELECT id, sighting_id, photo_id, caption FROM social_picks "
                    f"WHERE status = 'queued' ORDER BY {QUEUE_ORDER} FOR UPDATE"):
                if await is_postable(conn, row["sighting_id"], row["photo_id"]):
                    head = row
                    break
                await conn.execute(
                    "UPDATE social_picks SET last_error = $2 WHERE id = $1", row["id"], NOT_ELIGIBLE)
            if head is None:
                return RunResult("empty")
            if not enabled:
                log.info("dry run: would post pick=%s photo=%s caption=%r",
                         head["id"], head["photo_id"], head["caption"])
                return RunResult("dry_run", pick_id=head["id"], detail=head["caption"])
            key = await conn.fetchval("SELECT s3_key FROM photos WHERE id = $1", head["photo_id"])
            await conn.execute(
                "UPDATE social_picks SET status = 'posting', last_attempt_at = now() WHERE id = $1",
                head["id"])

    pick_id = head["id"]
    sent = False
    try:
        jpeg = await asyncio.to_thread(prepare_image, await storage.get(key))
        media_id = await client.upload_image(jpeg)
        sent = True
        post_id = await client.create_post(head["caption"], media_id)
    except Exception as exc:
        uncertain = sent and isinstance(exc, XUncertain)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE social_picks SET status = $2, last_error = $3 WHERE id = $1",
                pick_id, "posting" if uncertain else "queued", str(exc)[:500])
        log.warning("daily post failed pick=%s uncertain=%s: %s", pick_id, uncertain, exc)
        return RunResult("failed", pick_id=pick_id, detail=str(exc))

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE social_picks SET status = 'posted', x_post_id = $2, posted_at = now(), "
            "posted_on = $3, last_error = NULL WHERE id = $1",
            pick_id, post_id, today)
    log.info("posted pick=%s x_post_id=%s", pick_id, post_id)
    return RunResult("posted", pick_id=pick_id, x_post_id=post_id)
