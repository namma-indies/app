"""Transcription queue for the DGX worker: voice notes and clip sound tracks.

GPU API prefix: /internal/transcription-jobs. POST claim {owner}; POST /{id}/
heartbeat {lease_token}; POST /{id}/complete TranscriptCompletion; POST
/{id}/fail {lease_token, error, retryable}. Same bearer token and lease rules
as /internal/media-jobs, on its own table (migration 0020): a transcript is
never on the path to a sighting appearing or being matched, so it must not
share a queue with the work that is.

Nothing here interprets what was said. The transcript is stored for a later
step to suggest field values from, which an observer then confirms.
"""
import hmac
import json
import secrets
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, model_validator

from app.config import settings
from app.deps import get_storage
from app.ids import uuid7
from app.media_jobs import Claim, Failure, Lease, WireModel, require_gpu, token_hash

PIPELINE_VERSION = "whisper-cpp-large-v3-vad-v1"
MODEL_NAME = "ggml-large-v3"
MAX_TEXT = 20_000
MAX_SEGMENTS = 2_000
# Long enough for any clip the worker will read (it stops decoding at 10 min).
MAX_OFFSET_MS = 60 * 60 * 1000

router = APIRouter(prefix="/internal/transcription-jobs", dependencies=[Depends(require_gpu)])


class Segment(WireModel):
    start_ms: int = Field(ge=0, le=MAX_OFFSET_MS)
    end_ms: int = Field(ge=0, le=MAX_OFFSET_MS)
    text: str = Field(max_length=MAX_TEXT)


_CODE = r"^[a-z]{2,3}$"


class LanguageDiagnostics(WireModel):
    """How the worker settled the language (#173): codes and numbers only."""
    detected: str | None = Field(default=None, pattern=_CODE)
    chosen: str | None = Field(default=None, pattern=_CODE)
    # The likeliest languages and their probabilities, at most three.
    top: list[tuple[Annotated[str, Field(pattern=_CODE)], Annotated[float, Field(ge=0, le=1)]]] = Field(
        default_factory=list, max_length=3)


class TranscriptCompletion(Lease):
    pipeline_version: Literal[PIPELINE_VERSION]
    model: Literal[MODEL_NAME]
    outcome: Literal["transcribed", "no_speech", "no_audio"]
    # ISO 639-1 mostly; whisper also uses three-letter codes ("haw", "yue").
    language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}$")
    text: str = Field(default="", max_length=MAX_TEXT)
    segments: list[Segment] = Field(default_factory=list, max_length=MAX_SEGMENTS)
    # Sent only by a worker told this API takes it (`accepts` in the claim).
    language_diagnostics: LanguageDiagnostics | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.outcome == "transcribed":
            if not self.text.strip() or not self.segments or self.language is None:
                raise ValueError("a transcript needs text, segments and a language")
        elif self.text or self.segments:
            raise ValueError("only a transcribed outcome carries text")
        if self.outcome == "no_audio" and self.language is not None:
            raise ValueError("no sound track has no language")
        if any(s.end_ms < s.start_ms for s in self.segments):
            raise ValueError("segment ends before it starts")
        return self


def transcription_enabled() -> bool:
    """Whether a queued sound will be transcribed: the GPU worker and its
    transcription are both switched on."""
    return settings.media_jobs_enabled and settings.transcription_enabled


async def enqueue_transcription(conn, capture_id: UUID, source: str, s3_key: str,
                                note_id: UUID | None = None, *, hint: str | None = None,
                                forced: bool = False) -> None:
    """Queue one sound to transcribe, inside the upload's own transaction.

    `note_id` is a note added after the upload (app/voice_notes.py); the
    upload's own note and clip have none. `hint` and `forced` are the language
    to transcribe in (app/note_language.hint_for). A no-op unless transcription is
    switched on: with no worker to drain it, a queued row would only ever age.
    """
    if not transcription_enabled():
        return
    if source not in ("note", "clip"):
        raise ValueError("invalid transcription source")
    await conn.execute(
        """INSERT INTO transcription_jobs (id, capture_id, source, s3_key, pipeline_version, note_id,
                                           language_hint, language_forced)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
           ON CONFLICT ON CONSTRAINT transcription_jobs_one_per_sound DO NOTHING""",
        uuid7(), capture_id, source, s3_key, PIPELINE_VERSION, note_id, hint, forced and hint is not None,
    )


async def claim_transcription(conn, owner: str):
    token = secrets.token_urlsafe(32)
    lease_owner = "gpu:" + owner
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                           "transcription-worker:" + owner)
        if await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM transcription_jobs WHERE lease_owner=$1 "
                "AND status='running' AND lease_expires_at>clock_timestamp())", lease_owner):
            return None
        job = await conn.fetchrow(
            """SELECT * FROM transcription_jobs WHERE pipeline_version=$1
               AND ((status='pending' AND run_after<=now())
                    OR (status='running' AND lease_expires_at<=now()))
               ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1""", PIPELINE_VERSION)
        if job is None:
            return None
        if job["attempts"] >= settings.media_max_attempts:
            await conn.execute(
                "UPDATE transcription_jobs SET status='failed',lease_token_hash=NULL,"
                "lease_expires_at=NULL,last_error='lease retry limit reached',updated_at=now() "
                "WHERE id=$1", job["id"])
            return None
        job = await conn.fetchrow(
            """UPDATE transcription_jobs SET status='running', attempts=attempts+1,
               lease_token_hash=$2, lease_owner=$3,
               lease_expires_at=now()+$4*interval '1 second', updated_at=now()
               WHERE id=$1 RETURNING *""",
            job["id"], token_hash(token), lease_owner, settings.media_lease_s)
    return job, token


async def locked_transcription(conn, job_id, token, *, allow_done=False):
    job = await conn.fetchrow(
        "SELECT *, lease_expires_at>clock_timestamp() AS live FROM transcription_jobs "
        "WHERE id=$1 FOR UPDATE", job_id)
    valid = (job is not None and job["lease_token_hash"]
             and hmac.compare_digest(job["lease_token_hash"], token_hash(token))
             and (job["lease_owner"] or "").startswith("gpu:"))
    if not valid or not ((allow_done and job["status"] == "done")
                         or (job["status"] == "running" and job["live"])):
        raise HTTPException(409, "stale or invalid lease")
    return job


@router.post("/claim")
async def claim(body: Claim, request: Request, storage=Depends(get_storage)):
    async with request.app.state.pool.acquire() as conn:
        result = await claim_transcription(conn, body.owner)
        if result is None:
            return {"job": None}
        job, token = result
    url = await storage.url(job["s3_key"], expires_s=settings.media_url_s)
    return {"job": {"id": str(job["id"]), "kind": job["source"],
                    "pipeline_version": job["pipeline_version"], "model": MODEL_NAME,
                    "lease_token": token, "lease_expires_at": job["lease_expires_at"],
                    "source": {"url": url},
                    # The language to transcribe in (app/note_language.py). A worker
                    # from before these fields reads only the ones it knows.
                    "language_hint": job["language_hint"], "language_forced": job["language_forced"],
                    # What this API takes back besides the transcript (#173): a worker
                    # sends extras only when listed, as one from before would refuse them.
                    "accepts": ["language_diagnostics"]}}


@router.post("/{job_id}/heartbeat")
async def heartbeat(job_id: UUID, body: Lease, request: Request):
    async with request.app.state.pool.acquire() as conn, conn.transaction():
        await locked_transcription(conn, job_id, body.lease_token)
        expires = await conn.fetchval(
            "UPDATE transcription_jobs SET lease_expires_at=now()+$2*interval '1 second',"
            "updated_at=now() WHERE id=$1 RETURNING lease_expires_at",
            job_id, settings.media_lease_s)
    return {"lease_expires_at": expires}


@router.post("/{job_id}/complete")
async def complete(job_id: UUID, body: TranscriptCompletion, request: Request):
    async with request.app.state.pool.acquire() as conn, conn.transaction():
        # A replayed completion (the worker retries one whose reply it lost) is
        # acknowledged, never written twice: the unique key holds the first.
        job = await locked_transcription(conn, job_id, body.lease_token, allow_done=True)
        if job["status"] == "done":
            return {"status": "done"}
        await conn.execute(
            """INSERT INTO voice_transcripts (id, capture_id, source, pipeline_version, model,
                                              outcome, language, text, segments, suggest_status,
                                              note_id, language_diagnostics)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11,$12::jsonb)
               ON CONFLICT ON CONSTRAINT voice_transcripts_one_per_sound DO NOTHING""",
            uuid7(), job["capture_id"], job["source"], body.pipeline_version, body.model,
            body.outcome, body.language, body.text.strip(),
            json.dumps([s.model_dump() for s in body.segments]),
            # Nothing to suggest from silence or a mute clip (app/suggestions.py).
            "pending" if body.outcome == "transcribed" else "skipped", job["note_id"],
            body.language_diagnostics.model_dump_json() if body.language_diagnostics else None)
        await conn.execute(
            "UPDATE transcription_jobs SET status='done',lease_expires_at=NULL,last_error=NULL,"
            "updated_at=now() WHERE id=$1", job_id)
    return {"status": "done"}


@router.post("/{job_id}/fail")
async def fail(job_id: UUID, body: Failure, request: Request):
    async with request.app.state.pool.acquire() as conn, conn.transaction():
        job = await locked_transcription(conn, job_id, body.lease_token)
        retry = body.retryable and job["attempts"] < settings.media_max_attempts
        await conn.execute(
            """UPDATE transcription_jobs SET status=$2, last_error=$3,
               run_after=now()+$4*interval '1 second', lease_token_hash=NULL,
               lease_expires_at=NULL, updated_at=now() WHERE id=$1""",
            job_id, "pending" if retry else "failed", body.error,
            min(300, 2 ** min(job["attempts"], 8)))
    return {"status": "queued" if retry else "failed"}
