"""AI-written descriptions of the animal in a sighting nobody described (#160).

For a sighting with no typed note, no voice note and no speech in its clip,
the local model on the DGX looks at up to three photos of the animal (its own
crops on a multi-animal capture) and writes one or two lines about the animal
alone, with an ear notch or a condition to suggest if the photos show one.
The observer uses, edits or dismisses the description and answers each
suggestion as for a voice note (`detail_suggestions.description_id`).

Same pattern as the voice-note suggestions, and the same worker route: the
API builds the request, the worker runs it unchanged on its llama-server and
hands back the raw answer, and everything is checked here. Voice notes go
first, then new sightings, then backfill at `describe_backfill_per_hour`.
Only on the local model (`SUGGEST_VIA=worker`): a street photo is never sent
to a third party.

Privacy, a hard rule. Another observer's sighting is placed only to within a
kilometre, and a description must not undo that: it may say nothing about
places, landmarks, shop signs or any text, houses, people or vehicles. The
prompt says so, and `privacy_problem` drops any description that does not
comply, whole: never trimmed, as a trimmed sentence can still point
somewhere, and the words are never stored, only which rule they broke. In v1
a description is shown only to its observer.
"""
import json
import logging
import re
import secrets
import unicodedata
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.deps import require_observer
from app.config import settings
from app.deps import get_conn
from app.ids import uuid7
from app.media_jobs import token_hash
from app.photos import thumb_key

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_IMAGES = 3
MAX_CHARS = 240
MAX_SENTENCES = 2
MAX_EDITED_CHARS = 500
MAX_ATTEMPTS = 10
MAX_RETRY_DELAY_S = 6 * 3600
NOT_READY_DELAY_S = 60
MAX_REPLY_CHARS = 16 * 1024
# What a photo can be trusted to show. Sex is left out on purpose: from build
# alone the model would mostly be guessing. "none" for an ear notch is left
# out too: an ear turned away looks the same as one not notched.
PHOTO_CHOICES = {"ear_notch": ("left", "right"), "condition": ("healthy", "injured")}

PROMPT = """\
You describe a street dog or cat from photos an observer took of it in India. \
Write one or two short plain sentences in English about the animal's body \
only: coat colour and markings, build, any collar, any visible injury, \
wound, limp, mange or other condition, and an ear notch or tipped ear if one \
is clearly visible.

Never mention anything other than the animal itself. In particular never \
mention or hint at: where it is, roads, streets, buildings, houses, gates, \
walls, shops, temples or any other place or landmark; any sign, board, \
writing, text, letters or numbers; any person or part of a person; any \
vehicle. Do not use names, numbers or quotation marks. If you are unsure \
about something, leave it out.

Also suggest, only if the photos clearly show it:
- ear_notch: "left" or "right", if one ear is notched or tipped;
- condition: "injured" for a visible wound, limp, bleeding or mange; \
"healthy" only if the animal clearly looks well.
For each suggestion, copy into "quote" the exact words of your description \
that say it."""

# Words a description of the animal alone has no use for. Each names a place,
# a landmark, writing, a person or a vehicle; matched on whole words, case
# folded, with plurals and -ed/-ing forms. Cast wide on purpose: a harmless
# description dropped costs a suggestion; a revealing one shown costs privacy.
DENY = frozenset("""
road street lane cross junction signal highway flyover bridge footpath pavement sidewalk
temple mosque church shrine park market shop store stall hotel restaurant cafe bakery
school college hospital clinic office bank atm pharmacy station metro stop gate compound
building house home apartment society layout colony locality neighbourhood neighborhood
wall door window roof balcony porch garage parking terrace verandah veranda corner
near nearby beside outside opposite across behind
sign signboard board banner text written writing read reads letter lettering logo poster
plate number label graffiti name named called
person people man men woman women boy girl child children kid kids owner someone somebody
crowd lady ladies guy vendor guard watchman resident passerby hand hands
car bike scooter motorcycle motorbike auto rickshaw autorickshaw bus truck lorry van
cycle bicycle vehicle tyre tire wheel
""".split())

# And the only words it may use: a description of a street animal's body
# needs no others. A denylist alone cannot foresee every noun ("a pedestrian",
# "a taxi", a place's name at the start of a sentence); this can. A word not
# here drops the description, so the cost of a gap is a description not shown.
ALLOW = frozenset("""
a an the this that it its it's is are was has have had looks look appears appear seems seem
and or but with without on of in at to from by over under along around between both each
one two three some few several many no not very quite slightly rather somewhat mostly partly
also too still well clearly visibly possibly
dog dogs puppy pup cat cats kitten animal male female stray indie
coat fur hair haired coated body build frame head face muzzle snout nose mouth jaw teeth tongue
eye eyes ear ears eyebrow neck chest belly stomach back side flank hip rump shoulder shoulders
leg legs foreleg forelegs hind front rear paw paws foot feet toe toes claw claws nail nails
tail tip tips skin whisker whiskers patch patches spot spots spotted mark marks marking markings
stripe stripes striped streak blaze collar band tag harness rope string bell ribbon
colour color coloured colored brown black white tan fawn cream golden gold ginger orange red
grey gray silver beige yellow yellowish brownish reddish greyish blackish whitish dark light
blue green pink purple maroon
pale tricolour tricolor brindle brindled tabby calico tortoiseshell sable merle pied piebald
mixed spotted dappled speckled solid plain
short long medium thick thin thinning fluffy smooth rough shaggy curly wiry sleek glossy dull
dense sparse matted dirty clean patchy bald balding hairless
small little tiny large big medium sized size young old adult elderly senior lean slim slender
skinny bony underweight healthy fit sturdy stocky muscular heavy chubby fat plump well fed
notch notched tipped cut clipped missing torn droopy floppy erect upright pointed folded cropped
left right both
wound wounded injury injured cut cuts scratch scratches scar scars scarred sore sores lesion
lesions bleeding blood swollen swelling limp limps limping lame mange mangy rash infection
infected discharge tick ticks flea fleas lump lumps growth bump tumour tumor ulcer
lying sitting standing sleeping resting walking alert calm relaxed
""".split())

_POSSESSIVE = re.compile(r"(?<=[A-Za-z])['’](?=[A-Za-z])")
_ALLOWED = re.compile(r"[A-Za-z ,.;:!?\-]*")
_QUOTES = set("\"“”‘’«»()[]{}<>`'")


def _forms(word: str) -> set[str]:
    forms = {word}
    for suffix in ("s", "es", "ed", "d", "ing"):
        if word.endswith(suffix) and len(word) > len(suffix) + 2:
            forms.add(word[: -len(suffix)])
    return forms


def privacy_problem(text: str) -> str | None:
    """Which rule a description breaks, or None if it may be shown."""
    text = unicodedata.normalize("NFC", text).strip()
    if not text:
        return "empty"
    if len(text) > MAX_CHARS or len(re.findall(r"[.!?]+", text.rstrip(".!?"))) >= MAX_SENTENCES:
        return "too_long"
    if any(c.isdigit() for c in text):
        return "digit"
    plain = _POSSESSIVE.sub("", text)       # "dog's" is not a quotation
    if any(c in _QUOTES for c in plain):
        return "quotes"
    if not _ALLOWED.fullmatch(plain):
        return "script"                     # anything else is likelier copied than described
    for sentence in re.split(r"[.!?]+", plain):
        words = sentence.split()
        if any(w[0].isupper() for w in words[1:]):
            return "proper_noun"
    words = re.findall(r"[a-z]+", plain.lower())
    if any(_forms(word) & DENY for word in words):
        return "denied_word"
    if any(not _forms(word) & ALLOW for word in words):
        return "unlisted_word"
    return None


def response_schema() -> dict:
    item = {"field": {"type": "string", "enum": list(PHOTO_CHOICES)},
            "value": {"type": "string"}, "quote": {"type": "string"}}
    return {"type": "object", "additionalProperties": False, "required": ["description", "suggestions"],
            "properties": {
                "description": {"type": "string"},
                "suggestions": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": list(item),
                    "properties": item}}}}


def build_request(image_urls: list[str], species: str | None) -> dict:
    """The chat request, minus the model name; the worker inlines each image."""
    animal = species if species in ("dog", "cat") else "dog or cat"
    content: list[dict] = [{"type": "text", "text": f"The animal is a {animal}. Photos of it:"}]
    content += [{"type": "image_url", "image_url": {"url": url}} for url in image_urls]
    return {"messages": [{"role": "system", "content": PROMPT}, {"role": "user", "content": content}],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "animal_description", "strict": True, "schema": response_schema()}}}


def check_answer(content) -> tuple[str | None, str | None, list[dict]]:
    """(description, dropped rule, suggestions) from the model's raw answer.
    Raises ValueError for one that is not the agreed shape."""
    from app.suggestions import validate
    if not isinstance(content, str) or len(content) > MAX_REPLY_CHARS:
        raise ValueError("unexpected reply")
    answer = json.loads(content)
    description, raw = answer["description"], answer["suggestions"]
    if not isinstance(description, str) or not isinstance(raw, list):
        raise ValueError("unexpected reply")
    description = " ".join(description.split())
    problem = privacy_problem(description)
    if problem:
        return None, problem, []
    kept = [s for s in validate(raw, description)
            if s["value"] in PHOTO_CHOICES.get(s["field"], ())]
    return description, None, kept


def describing() -> bool:
    """Only with the local model draining the queue (see module docstring)."""
    return (settings.describe_enabled and settings.suggestions_enabled
            and settings.suggest_via == "worker")


async def enqueue(conn, sighting_id: UUID, *, backfill: bool = False) -> None:
    """Queue a sighting to describe, if nothing was noted about it at upload.
    Whether it still needs one is checked again when the worker asks (`plan`)."""
    if not describing() and not backfill:
        return
    await conn.execute(
        """INSERT INTO photo_descriptions (id, sighting_id, capture_id, backfill)
           SELECT $2, s.id, s.capture_id, $3 FROM sightings s JOIN captures c ON c.id = s.capture_id
           WHERE s.id = $1 AND btrim(coalesce(s.attrs->>'note', '')) = ''
             AND s.audio_s3_key IS NULL AND c.audio_s3_key IS NULL
           ON CONFLICT (sighting_id) DO NOTHING""",
        sighting_id, uuid7(), backfill)


async def claim(conn, lease_s: float, token: str):
    return await conn.fetchrow(
        """UPDATE photo_descriptions SET status='running', attempts=attempts+1,
               lease_until=now()+$1*interval '1 second', lease_token_hash=$2,
               first_claimed_at=COALESCE(first_claimed_at, now())
           WHERE id = (
               SELECT id FROM photo_descriptions
               WHERE run_after <= now()
                 AND (status='pending' OR (status='running' AND lease_until <= now()))
                 AND (NOT backfill OR (SELECT count(*) FROM photo_descriptions
                                       WHERE backfill AND first_claimed_at > now() - interval '1 hour') < $3)
               ORDER BY backfill, created_at, id FOR UPDATE SKIP LOCKED LIMIT 1)
           RETURNING id, sighting_id, capture_id, attempts, lease_until""",
        lease_s, token_hash(token), settings.describe_backfill_per_hour)


# A sound heard about this animal: the upload's own note or clip, or a note
# added later about the whole capture or this animal; not one about another
# animal of the capture (#156). $1 is the capture, $2 the sighting.
ABOUT_IT = "(n.id IS NULL OR n.sighting_id IS NULL OR n.sighting_id = $2)"


async def plan(conn, sighting_id: UUID):
    """("describe", [thumb keys], instance_id, species), or ("wait"|"skip", ...)."""
    s = await conn.fetchrow(
        """SELECT s.processing_state, s.attrs, s.species, s.audio_s3_key, s.capture_id,
                  c.audio_s3_key AS capture_audio, c.processing_state AS capture_state
           FROM sightings s JOIN captures c ON c.id = s.capture_id WHERE s.id = $1""", sighting_id)
    if s is None:
        return "skip", [], None, None
    if s["processing_state"] in ("queued", "processing"):
        return "wait", [], None, None
    if s["processing_state"] in ("failed", "no_animal"):
        return "skip", [], None, None
    attrs = json.loads(s["attrs"]) if isinstance(s["attrs"], str) else (s["attrs"] or {})
    if str(attrs.get("note") or "").strip() or s["audio_s3_key"] or s["capture_audio"]:
        return "skip", [], None, None          # they said or wrote something
    if await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM voice_notes WHERE capture_id=$1 AND (sighting_id IS NULL OR sighting_id=$2))",
            s["capture_id"], sighting_id):
        return "skip", [], None, None          # a note added since, about it
    if await conn.fetchval(
            f"""SELECT EXISTS(SELECT 1 FROM transcription_jobs j LEFT JOIN voice_notes n ON n.id = j.note_id
                WHERE j.capture_id=$1 AND j.status IN ('pending','running') AND {ABOUT_IT})""",
            s["capture_id"], sighting_id):
        return "wait", [], None, None          # the clip's sound may have words in it
    if await conn.fetchval(
            f"""SELECT EXISTS(SELECT 1 FROM voice_transcripts t LEFT JOIN voice_notes n ON n.id = t.note_id
                WHERE t.capture_id=$1 AND t.outcome='transcribed' AND {ABOUT_IT})""",
            s["capture_id"], sighting_id):
        return "skip", [], None, None
    if s["capture_state"] == "legacy":
        rows = await conn.fetch(
            "SELECT NULL::uuid AS instance_id, s3_key FROM photos WHERE sighting_id=$1 ORDER BY created_at, id LIMIT $2",
            sighting_id, MAX_IMAGES)
    else:
        # The animal's own crops: less of the street in them than the whole frame.
        rows = await conn.fetch(
            """SELECT a.id AS instance_id, p.s3_key FROM animal_instances a
               JOIN photos p ON p.id = a.evidence_photo_id
               WHERE a.sighting_id = $1 ORDER BY a.confidence DESC, a.id LIMIT $2""", sighting_id, MAX_IMAGES)
    if not rows:
        return "skip", [], None, None
    return "describe", [thumb_key(r["s3_key"]) for r in rows], rows[0]["instance_id"], s["species"]


async def _finish(pool, description_id, status, *, error=None, delay_s=0, refund=False):
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE photo_descriptions SET status=$2, error=$3, lease_until=NULL, lease_token_hash=NULL,
               run_after=now()+$4*interval '1 second', attempts=attempts-$5 WHERE id=$1""",
            description_id, status, error, delay_s, 1 if refund else 0)


async def worker_job(pool, storage) -> dict | None:
    """The next description for the worker, as a ready-made request, or None."""
    for _ in range(8):
        token = secrets.token_urlsafe(32)
        async with pool.acquire() as conn:
            row = await claim(conn, settings.media_lease_s, token)
            if row is None:
                return None
            kind, keys, _, species = await plan(conn, row["sighting_id"])
        if kind == "skip":
            await _finish(pool, row["id"], "skipped")
            continue
        if kind == "wait":
            await _finish(pool, row["id"], "pending", delay_s=NOT_READY_DELAY_S, refund=True)
            continue
        urls = await storage.urls(keys, expires_s=settings.media_url_s)
        return {"id": str(row["id"]), "kind": "describe", "lease_token": token,
                "lease_expires_at": row["lease_until"], "request": build_request(urls, species)}
    return None


async def owns(pool, job_id: UUID) -> bool:
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT EXISTS(SELECT 1 FROM photo_descriptions WHERE id=$1)", job_id)


async def _leased(conn, job_id: UUID, token: str):
    import hmac
    row = await conn.fetchrow(
        """SELECT id, sighting_id, capture_id, attempts, status, lease_token_hash,
                  lease_until > clock_timestamp() AS live FROM photo_descriptions WHERE id=$1 FOR UPDATE""", job_id)
    if (row is None or row["status"] != "running" or not row["live"] or not row["lease_token_hash"]
            or not hmac.compare_digest(row["lease_token_hash"], token_hash(token))):
        raise HTTPException(409, "stale or invalid lease")
    return row


async def heartbeat(pool, job_id: UUID, token: str):
    async with pool.acquire() as conn, conn.transaction():
        await _leased(conn, job_id, token)
        expires = await conn.fetchval(
            "UPDATE photo_descriptions SET lease_until=now()+$2*interval '1 second' WHERE id=$1 RETURNING lease_until",
            job_id, settings.media_lease_s)
    return {"lease_expires_at": expires}


async def _failed(pool, row, error: str, *, retryable=True):
    give_up = not retryable or row["attempts"] >= MAX_ATTEMPTS
    await _finish(pool, row["id"], "failed" if give_up else "pending", error=error,
                  delay_s=min(MAX_RETRY_DELAY_S, 30 * 2 ** row["attempts"]))


async def complete(pool, job_id: UUID, token: str, content: str):
    async with pool.acquire() as conn, conn.transaction():
        row = await _leased(conn, job_id, token)
        kind, _, instance_id, _ = await plan(conn, row["sighting_id"])
        await conn.execute("UPDATE photo_descriptions SET lease_until=now() WHERE id=$1", job_id)
    if kind != "describe":
        # A note arrived (or the sighting went) while the model was looking.
        await _finish(pool, job_id, "skipped")
        return {"status": "done"}
    try:
        description, dropped, suggested = check_answer(content)
    except (ValueError, KeyError, TypeError, AttributeError):
        await _failed(pool, row, "unparsable_reply")
        return {"status": "done"}
    # Counted, and the rule named; the words that broke it are never kept or logged.
    logger.info("description for sighting=%s: %s, %d suggestions", row["sighting_id"],
                f"dropped ({dropped})" if dropped else "kept", len(suggested))
    async with pool.acquire() as conn, conn.transaction():
        for s in suggested:
            await conn.execute(
                """INSERT INTO detail_suggestions (id, capture_id, description_id, instance_id, field, value, quote, model)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                   ON CONFLICT (description_id, instance_id, field) WHERE description_id IS NOT NULL DO NOTHING""",
                uuid7(), row["capture_id"], job_id, instance_id, s["field"], s["value"], s["quote"],
                settings.suggest_local_model)
        await conn.execute(
            """UPDATE photo_descriptions SET status='done', lease_until=NULL, lease_token_hash=NULL, error=NULL,
               description=$2, dropped=$3, model=$4 WHERE id=$1""",
            job_id, description, dropped, settings.suggest_local_model)
    return {"status": "done"}


async def fail(pool, job_id: UUID, token: str, error: str, retryable: bool):
    async with pool.acquire() as conn, conn.transaction():
        row = await _leased(conn, job_id, token)
    await _failed(pool, row, error, retryable=retryable)
    return {"status": "failed"}


async def for_sightings(conn, sighting_ids: list[UUID]) -> dict[UUID, dict]:
    """The description each sighting shows its observer, unless dismissed; or
    "describing" while a new one is still with the model, so the Journal knows
    to check back. Backfill is not announced: it may wait hours by design."""
    rows = await conn.fetch(
        """SELECT id, sighting_id, status, backfill, description, decision, decided_text FROM photo_descriptions
           WHERE sighting_id = ANY($1::uuid[])
             AND ((status='done' AND description IS NOT NULL AND decision <> 'dismissed')
                  OR (status IN ('pending','running') AND NOT backfill))""", sighting_ids)
    out = {}
    for r in rows:
        if r["status"] != "done":
            if describing():
                out[r["sighting_id"]] = {"id": str(r["id"]), "status": "describing", "text": None}
            continue
        out[r["sighting_id"]] = {"id": str(r["id"]), "status": r["decision"],
                                 "text": r["decided_text"] if r["decision"] == "edited" else r["description"]}
    return out


class DescriptionDecision(BaseModel):
    verdict: Literal["use", "edit", "dismiss"]
    text: str | None = Field(default=None, max_length=MAX_EDITED_CHARS)


@router.post("/description/{description_id}")
async def decide(description_id: UUID, body: DescriptionDecision,
                 observer_id: UUID = Depends(require_observer), conn=Depends(get_conn)):
    """Use it as written, edit it (the words are then the observer's), or dismiss it."""
    found = await conn.fetchval(
        """SELECT d.id FROM photo_descriptions d JOIN sightings s ON s.id = d.sighting_id
           WHERE d.id=$1 AND s.observer_id=$2 AND d.status='done' AND d.description IS NOT NULL""",
        description_id, observer_id)
    if found is None:
        raise HTTPException(404, "no such description")
    text = " ".join((body.text or "").split()) or None
    if body.verdict == "edit" and not text:
        raise HTTPException(422, "an edit needs the words")
    decision = {"use": "accepted", "edit": "edited", "dismiss": "dismissed"}[body.verdict]
    await conn.execute(
        """UPDATE photo_descriptions SET decision=$2, decided_text=$3, decided_by=$4, decided_at=$5 WHERE id=$1""",
        description_id, decision, text if decision == "edited" else None, observer_id, datetime.now(timezone.utc))
    return {"status": decision, "text": text if decision == "edited" else None}
