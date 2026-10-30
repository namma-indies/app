"""Today's model pass over transcripts made before it (#176).

Transcripts from before #153 have no English line, and some never got
suggestions. scripts/transcript_backfill.py queues them here, in one of two
modes, and the DGX worker reads them through the same /internal/suggestion-jobs
route as everything else, last of all and at most
TRANSCRIPT_BACKFILL_PER_HOUR an hour (0, the default, leaves the queue paused):

- "full": English (if not English) and suggestions, for a transcript whose
  suggestion pass never completed (skipped, or failed) and has none;
- "translate": English only, for any other transcript not in English and
  without it. Suggestions in the answer are dropped: one that already has
  suggestions, open or answered, is never asked again.

Suggestions are proposals as ever: shown only for details still blank, and
nothing in a field changes until its observer taps. The request and the
checks on the answer are app/suggestions.py's own.
"""
import hmac
import logging
import secrets
from uuid import UUID

from fastapi import HTTPException

from app.config import settings
from app.ids import uuid7
from app.media_jobs import token_hash

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 10
MAX_RETRY_DELAY_S = 6 * 3600
NOT_READY_DELAY_S = 60


def backfilling() -> bool:
    """Drained by the local model on the DGX, like descriptions."""
    from app.suggestions import worker_drains
    return worker_drains() and settings.transcript_backfill_per_hour > 0


async def enqueue(conn, transcript_id: UUID, mode: str) -> None:
    await conn.execute(
        """INSERT INTO transcript_backfill (id, transcript_id, mode) VALUES ($1,$2,$3)
           ON CONFLICT (transcript_id) DO NOTHING""", uuid7(), transcript_id, mode)


async def _claim(conn, lease_s: float, token: str):
    return await conn.fetchrow(
        """UPDATE transcript_backfill SET status='running', attempts=attempts+1,
               lease_until=now()+$1*interval '1 second', lease_token_hash=$2,
               first_claimed_at=COALESCE(first_claimed_at, now())
           WHERE id = (
               SELECT id FROM transcript_backfill
               WHERE run_after <= now() AND (status='pending' OR (status='running' AND lease_until <= now()))
                 AND (SELECT count(*) FROM transcript_backfill WHERE first_claimed_at > now() - interval '1 hour') < $3
               ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1)
           RETURNING id, transcript_id, mode, attempts, lease_until, error""",
        lease_s, token_hash(token), settings.transcript_backfill_per_hour)


async def _finish(pool, job_id, status, *, error=None, delay_s=0, refund=False):
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE transcript_backfill SET status=$2, error=$3, lease_until=NULL, lease_token_hash=NULL,
               run_after=now()+$4*interval '1 second', attempts=attempts-$5 WHERE id=$1""",
            job_id, status, error, delay_s, 1 if refund else 0)


async def worker_job(pool, storage) -> dict | None:
    """The next transcript to read again, as a ready-made request, or None."""
    from app.suggestions import build_request, plan, solo, wants_english
    for _ in range(8):
        token = secrets.token_urlsafe(32)
        async with pool.acquire() as conn:
            row = await _claim(conn, settings.media_lease_s, token)
            if row is None:
                return None
            t = await conn.fetchrow(
                "SELECT capture_id, note_id, language, text FROM voice_transcripts WHERE id=$1", row["transcript_id"])
            kind, animals = await plan(conn, t["capture_id"], t["note_id"]) if row["mode"] == "full" else ("single", None)
        # As in the normal queue: after an answer cut off (the worker's token
        # cap), English is left out so the suggestions can come back; for a
        # translate-only job that leaves nothing to ask, so it is skipped.
        english = wants_english(t["text"], t["language"], row["error"])
        if kind == "skip" or (row["mode"] == "translate" and not english):
            await _finish(pool, row["id"], "skipped")
            continue
        if kind == "wait":
            await _finish(pool, row["id"], "pending", delay_s=NOT_READY_DELAY_S, refund=True)
            continue
        urls = (await storage.urls([key for _, _, key in animals], expires_s=settings.media_url_s)
                if animals and not solo(animals) else [])
        labelled = [(label, url) for (label, _, _), url in zip(animals or [], urls)] or None
        return {"id": str(row["id"]), "kind": "suggest", "lease_token": token, "lease_expires_at": row["lease_until"],
                "request": build_request(t["text"], t["language"], labelled, english)}
    return None


async def owns(pool, job_id: UUID) -> bool:
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT EXISTS(SELECT 1 FROM transcript_backfill WHERE id=$1)", job_id)


async def _leased(conn, job_id: UUID, token: str):
    row = await conn.fetchrow(
        """SELECT b.id, b.transcript_id, b.mode, b.attempts, b.status, b.lease_token_hash,
                  b.lease_until > clock_timestamp() AS live
           FROM transcript_backfill b WHERE b.id=$1 FOR UPDATE""", job_id)
    if (row is None or row["status"] != "running" or not row["live"] or not row["lease_token_hash"]
            or not hmac.compare_digest(row["lease_token_hash"], token_hash(token))):
        raise HTTPException(409, "stale or invalid lease")
    return row


async def heartbeat(pool, job_id: UUID, token: str):
    async with pool.acquire() as conn, conn.transaction():
        await _leased(conn, job_id, token)
        expires = await conn.fetchval(
            "UPDATE transcript_backfill SET lease_until=now()+$2*interval '1 second' WHERE id=$1 RETURNING lease_until",
            job_id, settings.media_lease_s)
    return {"lease_expires_at": expires}


async def _failed(pool, row, error: str, *, retryable=True):
    give_up = not retryable or row["attempts"] >= MAX_ATTEMPTS
    await _finish(pool, row["id"], "failed" if give_up else "pending", error=error,
                  delay_s=min(MAX_RETRY_DELAY_S, 30 * 2 ** row["attempts"]))


async def complete(pool, job_id: UUID, token: str, content: str):
    from app.suggestions import UnparsableReply, _store, clean_english, parse_reply, plan
    async with pool.acquire() as conn, conn.transaction():
        row = await _leased(conn, job_id, token)
        t = await conn.fetchrow(
            "SELECT id, capture_id, note_id, language, text FROM voice_transcripts WHERE id=$1", row["transcript_id"])
        _, animals = await plan(conn, t["capture_id"], t["note_id"]) if row["mode"] == "full" else (None, None)
        await conn.execute("UPDATE transcript_backfill SET lease_until=now() WHERE id=$1", job_id)
    try:
        raw, english = parse_reply(content)
    except UnparsableReply:
        await _failed(pool, row, "unparsable_reply")
        return {"status": "done"}
    if row["mode"] == "translate" and clean_english(english, t["text"]) is None:
        # Asked for English and given none usable (empty, missing, the note
        # handed back): a failed attempt, not a job done, or it is gone for good.
        await _failed(pool, row, "unusable_translation")
        return {"status": "done"}
    # Translate-only: what it suggests is not asked for, and not kept.
    await _store(pool, dict(t), raw if row["mode"] == "full" else [], animals, settings.suggest_local_model, english)
    await _finish(pool, job_id, "done")
    return {"status": "done"}


async def fail(pool, job_id: UUID, token: str, error: str, retryable: bool):
    async with pool.acquire() as conn, conn.transaction():
        row = await _leased(conn, job_id, token)
    await _failed(pool, row, error, retryable=retryable)
    return {"status": "failed"}
