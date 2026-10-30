"""Suggested animal details from a voice transcript; the observer decides.

Each new transcript is read by a vision language model, one of two ways
(`settings.suggest_via`):

- "worker" (default): a Qwen3.8 running on the DGX slice next to whisper. The
  worker pulls a ready-made request from /internal/suggestion-jobs, runs it on
  its own llama-server on 127.0.0.1, and hands back the raw answer. Nothing
  leaves our infrastructure, and the worker still holds no third-party key.
- "api": the API's own background task calls an OpenAI-compatible endpoint.

Either way the request is built and the answer checked here, and what
survives is stored as `detail_suggestions` rows. Those are proposals only. Every proposal must quote the words that prompted it, and
a quote that does not occur in the transcript is dropped: a model that invents
evidence does not get to put a value in front of anyone.

Two kinds of upload, two places the observer answers:

- A /sighting upload has one animal and its details live on the sighting.
  The model reads the note alone; the Journal shows each suggestion as a
  blank to fill in, and `POST /suggestion/{id}` writes the option tapped.
- A multi-animal /capture: the model also sees one photo per animal found,
  labelled A, B, ..., and must say which animal each detail is about (or it
  is dropped). Its sightings' details are rebuilt from the review sheet on
  every save, so the sheet is where suggestions appear and saving it is the
  decision; `POST /suggestion/{id}` then only records the answer.
"""
import asyncio
import base64
import hmac
import json
import logging
import re
import secrets
import unicodedata
from datetime import datetime, timezone
from string import ascii_uppercase
from typing import Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth.deps import require_observer
from app.capture_contracts import AnimalDetails
from app.config import settings
from app.deps import get_conn, get_storage
from app.ids import uuid7
from app.media_jobs import Claim, Failure, Lease, require_gpu, token_hash
from app.photos import thumb_key

logger = logging.getLogger(__name__)
router = APIRouter()

# What a suggestion may say, per field. "unsure" is a choice an observer can
# make, never one worth suggesting.
CHOICES = {
    "sex": ("male", "female"),
    "ear_notch": ("none", "left", "right"),
    "condition": ("healthy", "injured"),
}
FIELDS = (*CHOICES, "known_name")
# Ten attempts, waits doubling from a minute and capped at six hours (about
# eight and a half hours in all): a worker outage should heal by itself once
# the worker is back, not strand every note uploaded while it was down (#116).
MAX_ATTEMPTS = 10
MAX_RETRY_DELAY_S = 6 * 3600
# More animals than this in one upload and a note is unlikely to tell them
# apart; the photos would also dominate the request's cost.
MAX_ANIMALS = 8
# How long to wait before looking again at a capture whose animals are still
# being found. Not an attempt: nothing failed.
NOT_READY_DELAY_S = 60
# Four fields with short quotes; anything longer is not an answer to this.
MAX_REPLY_CHARS = 64 * 1024
# Languages an observer here is expected to speak: English and the Indian
# languages whisper knows. Any other label is more likely a misdetection
# (English heard as Welsh, say) than a visitor, so the model is told so.
EXPECTED_LANGUAGES = frozenset(("en", "hi", "kn", "ta", "te", "ml", "mr", "bn", "gu", "pa", "ur"))
# A translation is a few sentences; more than this is not one.
MAX_ENGLISH_CHARS = 8000

SYSTEM_PROMPT = """\
You read a short spoken note an observer recorded while logging a street dog \
or cat in India. The note may be in English, Kannada, Hindi, Tamil or a mix.

Suggest values only for details the speaker actually states about the animal:
- sex: "male" or "female", only when a word in your quote marks it: a \
pronoun for the animal ("she is asleep", "he wags his tail") or a gendered \
word or verb ending. "This dog" or "it" says nothing about sex.
- ear_notch: "left", "right" or "none". An ear notch or tipped ear marks a \
sterilised animal. Only if the speaker mentions the ear.
- condition: "injured" if they describe a wound, limp, bleeding, mange or \
illness; "healthy" only if they say it looks healthy or fine.
- known_name: a name people call this animal, only if the speaker gives one.

Leave out anything not stated. Do not guess from tone or context. For each \
suggestion, copy the exact words from the note that support it into "quote", \
in the note's own language and script."""

ANIMALS_PROMPT = """

This upload shows several animals. A photo of each follows, labelled with a \
letter. For each suggestion, set "animal" to the letter of the animal the \
speaker means, using what they say about its look, colour, size or place to \
match it to a photo. If the note does not make clear which animal it is \
about, set "animal" to "unclear". Never infer a detail from the photos \
alone: a suggestion still needs the speaker's words."""

TRANSLATE_PROMPT = """

After the suggestions, put a faithful English translation of the whole note \
in "english". Translate only: add, explain and leave out nothing, and keep \
names as they were said. The quotes stay in the note's own language and \
script."""


def response_schema(labels: list[str] | None, english: bool = False) -> dict:
    item = {
        "field": {"type": "string", "enum": list(FIELDS)},
        "value": {"type": "string"},
        "quote": {"type": "string"},
    }
    if labels:
        item["animal"] = {"type": "string", "enum": [*labels, "unclear"]}
    # "english" after "suggestions": a constrained decoder writes properties
    # in this order, so the suggestions come out exactly as they did before.
    properties = {"suggestions": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": list(item), "properties": item,
    }}}
    if english:
        properties["english"] = {"type": "string"}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def _fold(text: str) -> tuple[str, list[int]]:
    """Words only, and where each kept character came from in `text`.

    Case, spacing, punctuation and zero-width joiners do not count: whisper
    punctuates and a model copying it often does not (a comma skipped, a curly
    apostrophe made straight); the words are the evidence. Letters, digits and
    combining marks stay, so Indic scripts keep their vowel signs. Characters
    are folded one by one so every kept character has an offset in the
    original, which `validate` uses to quote the note itself.
    """
    chars: list[str] = []
    origin: list[int] = []
    for at, char in enumerate(text):
        kind = unicodedata.category(char)
        if kind == "Cf":
            continue
        for folded in (char.casefold() if kind[0] in "LNM" else " "):
            if folded == " " and (not chars or chars[-1] == " "):
                continue
            chars.append(folded)
            origin.append(at)
    while chars and chars[-1] == " ":
        chars.pop()
        origin.pop()
    return "".join(chars), origin


def _normal(text: str) -> str:
    return _fold(unicodedata.normalize("NFC", text))[0]


def _spoken(transcript: str, quote: str) -> str | None:
    """The transcript's own words for `quote`, or None if it was not said.

    Quoted from the transcript, not the model, so the Journal can find and
    highlight them literally however the model punctuated its copy.
    """
    folded, origin = _fold(transcript)
    said = _normal(quote)
    at = folded.find(said) if said else -1
    if at < 0:
        return None
    return transcript[origin[at]:origin[at + len(said) - 1] + 1]


def clean_value(field: str, value) -> str | None:
    """The stored form of a value for this field, or None if it is not one."""
    if not isinstance(value, str):
        return None
    if field == "known_name":
        try:
            return AnimalDetails(known_name=value).known_name
        except ValueError:
            return None
    value = value.strip().lower()
    return value if value in CHOICES.get(field, ()) else None


def validate(raw: list, transcript: str, labels: list[str] | None = None) -> list[dict]:
    """The model's suggestions that can be shown: at most one per field (and,
    with several animals, per animal), each quoting the note and, if there are
    labels, naming one animal."""
    # Offsets are into the transcript as stored; one that is not in NFC (whisper
    # emits NFC) falls back to the model's own copy of the words.
    composed = unicodedata.normalize("NFC", transcript)
    kept: dict[tuple, dict] = {}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        field, quote = item.get("field"), item.get("quote")
        if field not in FIELDS or not isinstance(quote, str):
            continue
        quote = quote.strip()
        if not quote or len(quote) > 500:
            continue
        spoken = _spoken(composed, quote)
        if spoken is None:
            continue
        if composed == transcript:
            quote = spoken
        animal = None
        if labels:
            animal = item.get("animal")
            if animal not in labels:  # includes "unclear"
                continue
        value = clean_value(field, item.get("value"))
        # A name is only as good as the words it came from: it must be in them,
        # not a respelling the model made up.
        if field == "known_name" and value and _normal(value) not in _normal(quote):
            continue
        if not value or (field, animal) in kept:
            continue
        kept[(field, animal)] = {"field": field, "value": value, "quote": quote, "animal": animal}
    return list(kept.values())


def wants_english(text: str, language: str | None, last_error: str | None = None) -> bool:
    """Whether to ask for a translation along with the suggestions.

    Not for English, not past the length cap (see settings), and not again
    after an answer that could not be parsed: a translation cut off by the
    worker's token cap must not keep costing the note its suggestions.
    """
    return (language != "en" and 0 < len(text) <= settings.suggest_translate_max_chars
            and last_error != "unparsable_reply")


def clean_english(value, transcript: str) -> str | None:
    """The translation to keep, or None: a string, not empty, not huge, and
    not the note handed back unchanged."""
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    if not value or len(value) > MAX_ENGLISH_CHARS or _normal(value) == _normal(transcript):
        return None
    return value


def build_request(transcript: str, language: str, animals: list[tuple[str, str]] | None = None,
                  english: bool = False) -> dict:
    """The chat request for one transcript, minus the model name.

    `animals` is (label, image URL) per animal on a multi-animal upload: a
    data: URL on the OpenAI path, a short-lived storage URL when the DGX worker
    runs the model (it downloads and inlines each one; its model never fetches).
    Built here for both, so the prompt and the checks on the answer live in
    one place whichever model reads it. `english` also asks for a translation
    (see `wants_english`).
    """
    labels = [label for label, _ in animals] if animals else None
    detected = f"Detected language: {language}"
    if language not in EXPECTED_LANGUAGES:
        detected += " (probably misdetected; read the note for what it says)"
    content: list[dict] = [{"type": "text", "text": f"{detected}\nNote:\n{transcript}"}]
    for label, url in animals or []:
        content.append({"type": "text", "text": f"Animal {label}:"})
        content.append({"type": "image_url", "image_url": {"url": url}})
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT + (ANIMALS_PROMPT if labels else "")
             + (TRANSLATE_PROMPT if english else "")},
            {"role": "user", "content": content},
        ],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "animal_details", "strict": True, "schema": response_schema(labels, english)}},
    }


class UnparsableReply(ValueError):
    """The model answered, but not in the agreed shape: cut off, say, by a
    token cap. Recorded as "unparsable_reply" on either path, which is what
    makes the retry leave the translation out (`wants_english`)."""


def parse_reply(content) -> tuple[list, object]:
    """(suggestions, english) from the model's answer; english is None when
    not asked for, and still unchecked (see `clean_english`)."""
    try:
        if not isinstance(content, str) or len(content) > MAX_REPLY_CHARS:
            raise ValueError("unexpected reply")
        answer = json.loads(content)
        suggestions = answer["suggestions"]
        if not isinstance(suggestions, list):
            raise ValueError("unexpected reply")
        return suggestions, answer.get("english")
    except (ValueError, KeyError, TypeError, AttributeError):
        raise UnparsableReply() from None


async def ask_model(client: httpx.AsyncClient, transcript: str, language: str,
                    animals: list[tuple[str, bytes]] | None = None,
                    english: bool = False) -> tuple[list, object]:
    """The OpenAI path. `animals` is (label, WebP thumbnail) per animal."""
    body = build_request(transcript, language, [
        (label, "data:image/webp;base64," + base64.b64encode(image).decode())
        for label, image in animals or []] or None, english)
    body["model"] = settings.suggest_model
    if settings.suggest_reasoning_effort:
        body["reasoning_effort"] = settings.suggest_reasoning_effort
    response = await client.post(
        settings.suggest_base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": "Bearer " + settings.suggest_api_key},
        json=body, timeout=settings.suggest_timeout_s,
    )
    response.raise_for_status()
    return parse_reply(response.json()["choices"][0]["message"]["content"])


async def claim(conn, lease_s: float, token: str | None = None):
    """One transcript to suggest from, leased by time; never held open.
    `token` fences a lease held by the DGX worker."""
    return await conn.fetchrow(
        """UPDATE voice_transcripts SET suggest_status='running',
               suggest_attempts=suggest_attempts+1,
               suggest_lease_until=now()+$1*interval '1 second',
               suggest_lease_token_hash=$2
           WHERE id = (
               SELECT id FROM voice_transcripts
               WHERE outcome='transcribed' AND suggest_run_after<=now()
                 AND (suggest_status='pending'
                      OR (suggest_status='running' AND suggest_lease_until<=now()))
               ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1)
           RETURNING id, capture_id, language, text, suggest_attempts, suggest_error""",
        lease_s, token_hash(token) if token else None,
    )


async def plan(conn, capture_id: UUID):
    """What to show the model for this capture.

    ("single", None) for a /sighting upload; ("animals", [(label, instance_id,
    thumb key)]) for a multi-animal capture whose animals are known;
    ("wait", None) while they are still being found; ("skip", None) when there
    is nothing to attach a suggestion to.
    """
    capture = await conn.fetchrow("SELECT processing_state FROM captures WHERE id=$1", capture_id)
    if capture is None:
        return "skip", None
    state = capture["processing_state"]
    if state == "legacy":
        has_sighting = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM sightings WHERE id=$1)", capture_id)
        return ("single", None) if has_sighting else ("skip", None)
    if state in ("queued", "processing"):
        return "wait", None
    if state not in ("needs_review", "ready"):
        return "skip", None
    # One photo per animal the tracker followed: its most confident box.
    rows = await conn.fetch(
        """SELECT DISTINCT ON (a.track_id) a.id, p.s3_key FROM animal_instances a
           JOIN photos p ON p.id = a.evidence_photo_id
           WHERE a.capture_id = $1 ORDER BY a.track_id, a.confidence DESC, a.id""", capture_id)
    if not rows or len(rows) > MAX_ANIMALS:
        return "skip", None
    return "animals", [(ascii_uppercase[i], r["id"], thumb_key(r["s3_key"])) for i, r in enumerate(rows)]


def solo(animals) -> UUID | None:
    """The one animal's instance id when an upload has exactly one, else None.

    Nothing needs telling apart then, so the model reads the note alone, as for
    a /sighting: no photo to fetch (and none of the image path's ways to fail),
    no letter to pick and no "unclear" to answer. What survives belongs to that
    animal.
    """
    return animals[0][1] if animals and len(animals) == 1 else None


async def _finish(pool, transcript_id, status, *, error=None, delay_s=0, refund=False):
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE voice_transcripts SET suggest_status=$2, suggest_error=$3,
               suggest_lease_until=NULL, suggest_lease_token_hash=NULL,
               suggest_run_after=now()+$4*interval '1 second',
               suggest_attempts=suggest_attempts-$5 WHERE id=$1""",
            transcript_id, status, error, delay_s, 1 if refund else 0)


async def _failed(pool, transcript_id, attempts, error: str, *, retryable=True):
    give_up = not retryable or attempts >= MAX_ATTEMPTS
    await _finish(pool, transcript_id, "failed" if give_up else "pending", error=error,
                  delay_s=min(MAX_RETRY_DELAY_S, 30 * 2 ** attempts))


async def _store(pool, row, raw, animals, model: str, english=None):
    """Check the model's answer against the note and keep what survives,
    with its translation of the note if it gave a usable one."""
    # With one animal the model was never asked to pick one (see `solo`).
    only = solo(animals)
    labels = [label for label, _, _ in animals] if animals and not only else None
    suggestions = validate(raw, row["text"], labels)
    instance_of = {None: only} if only else {label: instance for label, instance, _ in animals or []}
    english = clean_english(english, row["text"]) if row["language"] != "en" else None
    logger.info("suggestions for transcript=%s: model gave %d, kept %d; english=%s", row["id"],
                len(raw) if isinstance(raw, list) else 0, len(suggestions), english is not None)
    async with pool.acquire() as conn, conn.transaction():
        for s in suggestions:
            await conn.execute(
                """INSERT INTO detail_suggestions (id, capture_id, transcript_id, instance_id,
                                                   field, value, quote, model)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                   ON CONFLICT (transcript_id, instance_id, field) DO NOTHING""",
                uuid7(), row["capture_id"], row["id"], instance_of.get(s["animal"]),
                s["field"], s["value"], s["quote"], model)
        await conn.execute(
            "UPDATE voice_transcripts SET suggest_status='done',suggest_lease_until=NULL,"
            "suggest_lease_token_hash=NULL,suggest_error=NULL,"
            "english=COALESCE($2,english),english_model=CASE WHEN $2::text IS NULL "
            "THEN english_model ELSE $3 END WHERE id=$1", row["id"], english, model)


async def run_once(pool, storage, client: httpx.AsyncClient) -> bool:
    """One pass of the OpenAI path, run by the API's own consumer task."""
    async with pool.acquire() as conn:
        row = await claim(conn, settings.suggest_timeout_s * 2)
        if row is None:
            return False
        kind, animals = await plan(conn, row["capture_id"])
    if kind == "skip":
        await _finish(pool, row["id"], "skipped")
        return True
    if kind == "wait":
        await _finish(pool, row["id"], "pending", delay_s=NOT_READY_DELAY_S, refund=True)
        return True
    try:
        images = [(label, await storage.get(key)) for label, _, key in animals] if animals and not solo(animals) else None
        raw, english = await ask_model(client, row["text"], row["language"], images,
                                       wants_english(row["text"], row["language"], row["suggest_error"]))
    except Exception as error:
        # Logged by type only: a response body could echo the transcript.
        reason = "unparsable_reply" if isinstance(error, UnparsableReply) else type(error).__name__
        logger.warning("suggestions failed for transcript=%s: %s", row["id"], reason)
        await _failed(pool, row["id"], row["suggest_attempts"], reason)
        return True
    await _store(pool, row, raw, animals, settings.suggest_model, english)
    return True


async def consumer(pool, storage):
    async with httpx.AsyncClient(trust_env=False) as client:
        while True:
            try:
                busy = await run_once(pool, storage, client)
            except Exception:
                logger.warning("suggestion consumer pass failed", exc_info=True)
                busy = False
            if not busy:
                await asyncio.sleep(settings.suggest_poll_s)


# ---------------------------------------------------------------------------
# The DGX path: the worker pulls a ready-made request, runs it on its own
# local model, and hands back the raw answer to be checked here.
# ---------------------------------------------------------------------------

worker_router = APIRouter(prefix="/internal/suggestion-jobs", dependencies=[Depends(require_gpu)])


class Reply(Lease):
    # The model's message content, unparsed: checking it is this side's job.
    content: str = Field(max_length=MAX_REPLY_CHARS)


async def _leased(conn, transcript_id: UUID, token: str):
    row = await conn.fetchrow(
        """SELECT id, capture_id, language, text, suggest_attempts, suggest_status,
                  suggest_lease_token_hash, suggest_lease_until > clock_timestamp() AS live
           FROM voice_transcripts WHERE id=$1 FOR UPDATE""", transcript_id)
    if (row is None or row["suggest_status"] != "running" or not row["live"]
            or not row["suggest_lease_token_hash"]
            or not hmac.compare_digest(row["suggest_lease_token_hash"], token_hash(token))):
        raise HTTPException(409, "stale or invalid lease")
    return row


def worker_drains() -> bool:
    return settings.suggestions_enabled and settings.suggest_via == "worker"


@worker_router.post("/claim")
async def worker_claim(body: Claim, request: Request, storage=Depends(get_storage)):
    if not worker_drains():
        return {"job": None}
    pool = request.app.state.pool
    # A few transcripts may need skipping or deferring before one is ready.
    for _ in range(8):
        token = secrets.token_urlsafe(32)
        async with pool.acquire() as conn:
            row = await claim(conn, settings.media_lease_s, token)
            if row is None:
                return {"job": None}
            kind, animals = await plan(conn, row["capture_id"])
            expires = await conn.fetchval("SELECT suggest_lease_until FROM voice_transcripts WHERE id=$1", row["id"])
        if kind == "skip":
            await _finish(pool, row["id"], "skipped")
            continue
        if kind == "wait":
            await _finish(pool, row["id"], "pending", delay_s=NOT_READY_DELAY_S, refund=True)
            continue
        urls = (await storage.urls([key for _, _, key in animals], expires_s=settings.media_url_s)
                if animals and not solo(animals) else [])
        labelled = [(label, url) for (label, _, _), url in zip(animals or [], urls)] or None
        return {"job": {"id": str(row["id"]), "kind": "suggest", "lease_token": token,
                        "lease_expires_at": expires,
                        "request": build_request(row["text"], row["language"], labelled,
                                                 wants_english(row["text"], row["language"],
                                                               row["suggest_error"]))}}
    return {"job": None}


@worker_router.post("/{transcript_id}/heartbeat")
async def worker_heartbeat(transcript_id: UUID, body: Lease, request: Request):
    async with request.app.state.pool.acquire() as conn, conn.transaction():
        await _leased(conn, transcript_id, body.lease_token)
        expires = await conn.fetchval(
            "UPDATE voice_transcripts SET suggest_lease_until=now()+$2*interval '1 second' "
            "WHERE id=$1 RETURNING suggest_lease_until", transcript_id, settings.media_lease_s)
    return {"lease_expires_at": expires}


@worker_router.post("/{transcript_id}/complete")
async def worker_complete(transcript_id: UUID, body: Reply, request: Request):
    pool = request.app.state.pool
    async with pool.acquire() as conn, conn.transaction():
        row = await _leased(conn, transcript_id, body.lease_token)
        _, animals = await plan(conn, row["capture_id"])
        # Fence the lease before the slower checks: a second completion of the
        # same lease now finds it no longer running.
        await conn.execute("UPDATE voice_transcripts SET suggest_lease_until=now() WHERE id=$1", transcript_id)
    try:
        raw, english = parse_reply(body.content)
    except UnparsableReply:
        # The worker's part is done either way; an answer that is not the
        # agreed shape counts as a failed attempt here, not there.
        await _failed(pool, transcript_id, row["suggest_attempts"], "unparsable_reply")
        return {"status": "done"}
    await _store(pool, row, raw, animals, settings.suggest_local_model, english)
    return {"status": "done"}


@worker_router.post("/{transcript_id}/fail")
async def worker_fail(transcript_id: UUID, body: Failure, request: Request):
    pool = request.app.state.pool
    async with pool.acquire() as conn, conn.transaction():
        row = await _leased(conn, transcript_id, body.lease_token)
    await _failed(pool, transcript_id, row["suggest_attempts"], body.error, retryable=body.retryable)
    return {"status": "failed"}


def suggestions_running() -> bool:
    """Whether the API's own consumer drains the queue (the OpenAI path)."""
    return settings.suggestions_enabled and settings.suggest_via == "api" and bool(settings.suggest_api_key)


def answered(rows, held) -> list[dict]:
    """The suggestions in `rows` that are no longer open, as the transcript should show them.

    An answered one is reported with its recorded status and chosen value. One
    never answered but whose field the observer filled in by hand with the same
    value is "filled": the words still led to what is saved. An open one whose
    field holds something else (or nothing) is not here, it is still open or
    was overridden. `held(row)` is what that animal's field holds now.
    """
    out = []
    for row in rows:
        status = row["status"]
        if status == "open":
            current = held(row)
            if not current or _normal(str(current)) != _normal(row["value"]):
                continue
            status, chosen = "filled", current
        else:
            chosen = row["decided_value"]
        out.append({"id": row["id"], "instance_id": row["instance_id"], "field": row["field"],
                    "value": row["value"], "quote": row["quote"], "status": status, "chosen": chosen})
    return out


async def for_capture(conn, capture_id: UUID, details: dict) -> tuple[list[dict], list[dict]]:
    """A multi-animal capture's suggestions for its review sheet: (open, answered).

    One query for both. `details` maps instance id to what that animal holds now.
    """
    rows = await conn.fetch(
        "SELECT id, instance_id, field, value, quote, status, decided_value FROM detail_suggestions "
        "WHERE capture_id=$1 AND instance_id IS NOT NULL ORDER BY field, id", capture_id)
    opened = [{k: r[k] for k in ("id", "instance_id", "field", "value", "quote")} for r in rows if r["status"] == "open"]
    return opened, answered(rows, lambda r: details.get(r["instance_id"], {}).get(r["field"]))


class Decision(BaseModel):
    # The option the observer chose. The suggested value is "accepted", any
    # other is "corrected"; "dismiss" closes it without a value.
    verdict: Literal["choose", "dismiss"]
    value: str | None = None


@router.post("/suggestion/{suggestion_id}")
async def decide(suggestion_id: UUID, body: Decision,
                 observer_id: UUID = Depends(require_observer), conn=Depends(get_conn)):
    async with conn.transaction():
        row = await conn.fetchrow(
            """SELECT d.*, c.processing_state FROM detail_suggestions d
               JOIN captures c ON c.id = d.capture_id
               WHERE d.id = $1 AND c.observer_id = $2 FOR UPDATE OF d""",
            suggestion_id, observer_id)
        if row is None:
            raise HTTPException(404, "no such suggestion")
        if row["status"] != "open":
            raise HTTPException(409, "already decided")
        field = row["field"]
        if body.verdict == "dismiss":
            status, value = "dismissed", None
        else:
            # "unsure" is never suggested but is always the observer's to pick.
            unsure = field != "known_name" and body.value == "unsure"
            value = "unsure" if unsure else clean_value(field, body.value)
            if not value:
                raise HTTPException(422, "not an option for this detail")
            status = "accepted" if value == row["value"] else "corrected"
            if row["instance_id"] is None:
                # A /sighting upload: the tap is the edit. (A multi-animal
                # capture's review save already wrote the value.)
                attrs = await conn.fetchval(
                    "SELECT attrs FROM sightings WHERE id=$1 FOR UPDATE", row["capture_id"])
                if attrs is None:
                    raise HTTPException(404, "no such sighting")
                attrs = json.loads(attrs) if isinstance(attrs, str) else dict(attrs)
                attrs[field] = value
                await conn.execute("UPDATE sightings SET attrs=$2::jsonb, updated_at=now() WHERE id=$1",
                                   row["capture_id"], json.dumps(attrs))
        await conn.execute(
            """UPDATE detail_suggestions SET status=$2, decided_value=$3, decided_by=$4,
               decided_at=$5 WHERE id=$1""",
            suggestion_id, status, value, observer_id, datetime.now(timezone.utc))
    return {"status": status, "field": field, "value": value}
