"""Deleting what someone logged: one animal, or a whole upload (#54, #179).

Two units, because that is what an observer means:

- **a capture** is the upload -- the photo or clip they took, every animal in
  it, its voice notes, transcripts, suggestions and descriptions. "Delete
  photo" deletes this. A /sighting upload is a capture of its own (0015), so
  this is also how a single-animal photo goes.
- **a sighting** is one animal in it. "Remove this dog" deletes that animal's
  sighting, its boxes and its crops, and leaves the photo for the other
  animals in it. Removing the last animal deletes the whole capture.

Deleting is a hard delete of the rows, in one transaction, and a record of
what the object store owes (`media_purges`) written in that same transaction.
The store is asked after the transaction commits; what it refuses waits for
scripts/purge_media.py, exactly as a deleted voice note's audio does (#159).
A whole capture is owed by prefix, so thumbnails and crops nobody stored a key
for go with it.

What deletion does to everyone else's records is decided by the foreign keys,
and every key into the tables this deletes from is listed in `REFERENCES`
with the rule it must have. tests/test_erasure_guard.py fails when a migration
adds a key that is not listed, or changes a rule, so nothing new can quietly
outlive -- or block -- a deletion.

LOCK ORDER
----------
Workers finishing a job lock their job row, then the matching lock, then the
capture or sighting. A suggestion being answered locks the suggestion, then
the sighting. Erasure takes every one of those in that order, before it
deletes anything, so it waits for them rather than deadlocking with them:

    transcription_jobs, jobs, transcript_backfill, photo_descriptions,
    voice_transcripts, detail_suggestions, matching, social_picks,
    captures, sightings

A job finishing after its capture is gone finds no job row and gets 409.
"""
import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

import asyncpg

from app.config import settings
from app.ids import uuid7
from app.photos import thumb_key

logger = logging.getLogger(__name__)

# Every foreign key into a table erasure deletes from, and the ON DELETE rule
# it must have: "c" CASCADE (the row goes with it), "n" SET NULL (the row is
# someone else's and stays), "a" no action (erasure deletes the referencing
# rows itself, first). Checked against the database by the guard test.
REFERENCES: dict[tuple[str, str, str], str] = {
    # -> sightings
    ("sightings", "duplicate_of", "sightings"): "n",        # another's copy comes back (0016)
    ("photos", "sighting_id", "sightings"): "c",
    ("match_proposals", "sighting_id", "sightings"): "c",
    ("match_proposals", "candidate_sighting_id", "sightings"): "c",
    ("confirmations", "sighting_id", "sightings"): "c",
    ("clinical_records", "sighting_id", "sightings"): "n",  # about the animal (0031)
    ("jobs", "sighting_id", "sightings"): "c",
    ("sighting_reports", "sighting_id", "sightings"): "c",
    ("animal_instances", "sighting_id", "sightings"): "n",  # erasure deletes them itself
    ("auto_links", "sighting_id", "sightings"): "c",
    ("auto_links", "matched_sighting_id", "sightings"): "n",
    ("social_picks", "sighting_id", "sightings"): "c",      # a posted one is owed a delete on X
    ("voice_notes", "sighting_id", "sightings"): "c",       # its audio is owed by key or prefix
    ("photo_descriptions", "sighting_id", "sightings"): "c",
    # -> captures
    ("sightings", "capture_id", "captures"): "a",           # sightings are deleted first
    ("photos", "capture_id", "captures"): "c",
    ("jobs", "capture_id", "captures"): "c",
    ("animal_instances", "capture_id", "captures"): "c",
    ("transcription_jobs", "capture_id", "captures"): "c",
    ("voice_transcripts", "capture_id", "captures"): "c",
    ("detail_suggestions", "capture_id", "captures"): "c",
    ("voice_notes", "capture_id", "captures"): "c",
    ("photo_descriptions", "capture_id", "captures"): "c",
    # -> photos
    ("embeddings", "photo_id", "photos"): "c",
    ("detections", "photo_id", "photos"): "c",
    ("animal_instances", "source_photo_id", "photos"): "c",
    ("animal_instances", "evidence_photo_id", "photos"): "c",
    ("social_picks", "photo_id", "photos"): "c",
    # -> animal_instances
    ("embeddings", "instance_id", "animal_instances"): "c",
    ("detail_suggestions", "instance_id", "animal_instances"): "c",
    # -> voice_notes, voice_transcripts, photo_descriptions
    ("transcription_jobs", "note_id", "voice_notes"): "c",
    ("voice_transcripts", "note_id", "voice_notes"): "c",
    ("detail_suggestions", "transcript_id", "voice_transcripts"): "n",  # an answer outlives its note (0025)
    ("transcript_backfill", "transcript_id", "voice_transcripts"): "c",
    ("detail_suggestions", "description_id", "photo_descriptions"): "c",
    # -> match_proposals
    ("confirmations", "proposal_id", "match_proposals"): "n",  # a verdict outlives its pair (0031)
}

# Tables erasure deletes rows from, directly or by cascade.
ERASED_TABLES = sorted({target for (_, _, target) in REFERENCES})

DEADLOCK_RETRIES = 3


class LedgerUnavailable(Exception):
    """The erasure ledger could not be written, so nothing was erased."""


@dataclass
class Erased:
    """What one erasure owes once its transaction has committed."""
    kind: str                      # "capture" | "sighting"
    id: UUID
    media: list[tuple[str, str]] = field(default_factory=list)   # (key or prefix, kind)
    x_posts: list[str] = field(default_factory=list)
    # Picks whose post was started and never confirmed: (pick id, caption).
    unconfirmed_posts: list[tuple[UUID, str | None]] = field(default_factory=list)


def _prefixes(capture_id: UUID, sighting_ids) -> list[str]:
    # A /capture upload lives under captures/{id}/; a /sighting upload, and a
    # legacy media job's frames, under sightings/{sid}/ (its capture shares its id).
    return [f"captures/{capture_id}/"] + [f"sightings/{sid}/" for sid in sorted(sighting_ids, key=str)]


def _photo_keys(keys) -> list[str]:
    return [k for key in keys if key for k in (key, thumb_key(key))]


async def _lock(conn, capture_id: UUID) -> asyncpg.Record | None:
    """Everything a deletion of this capture touches, in LOCK ORDER."""
    from app.matching import lock_matching
    await conn.execute("SELECT 1 FROM transcription_jobs WHERE capture_id=$1 ORDER BY id FOR UPDATE", capture_id)
    await conn.execute("""SELECT 1 FROM jobs WHERE capture_id=$1
        OR sighting_id IN (SELECT id FROM sightings WHERE capture_id=$1) ORDER BY id FOR UPDATE""", capture_id)
    await conn.execute("""SELECT 1 FROM transcript_backfill b JOIN voice_transcripts t ON t.id=b.transcript_id
        WHERE t.capture_id=$1 ORDER BY b.id FOR UPDATE OF b""", capture_id)
    await conn.execute("SELECT 1 FROM photo_descriptions WHERE capture_id=$1 ORDER BY id FOR UPDATE", capture_id)
    await conn.execute("SELECT 1 FROM voice_transcripts WHERE capture_id=$1 ORDER BY id FOR UPDATE", capture_id)
    await conn.execute("SELECT 1 FROM detail_suggestions WHERE capture_id=$1 ORDER BY id FOR UPDATE", capture_id)
    await lock_matching(conn)
    await conn.execute("""SELECT 1 FROM social_picks WHERE sighting_id IN
        (SELECT id FROM sightings WHERE capture_id=$1) ORDER BY id FOR UPDATE""", capture_id)
    capture = await conn.fetchrow("SELECT * FROM captures WHERE id=$1 FOR UPDATE", capture_id)
    await conn.execute("SELECT 1 FROM sightings WHERE capture_id=$1 ORDER BY id FOR UPDATE", capture_id)
    return capture


async def _before(conn, sighting_ids: list[UUID], erased: Erased) -> tuple[list[UUID], list[UUID]]:
    """Read what the delete will change for everyone else, while it still exists.

    Returns (individuals the sightings were part of, other sightings that had a
    pending proposal with one of them)."""
    individuals = [r["individual_id"] for r in await conn.fetch(
        "SELECT DISTINCT individual_id FROM sightings WHERE id=ANY($1::uuid[]) AND individual_id IS NOT NULL",
        sighting_ids)]
    partners = [r["other"] for r in await conn.fetch(
        """SELECT DISTINCT CASE WHEN sighting_id=ANY($1::uuid[]) THEN candidate_sighting_id
                                ELSE sighting_id END AS other
           FROM match_proposals WHERE status='pending'
             AND (sighting_id=ANY($1::uuid[]) OR candidate_sighting_id=ANY($1::uuid[]))""", sighting_ids)
        if r["other"] is not None and r["other"] not in sighting_ids]
    for pick in await conn.fetch(
            "SELECT id, status, x_post_id, caption FROM social_picks WHERE sighting_id=ANY($1::uuid[])",
            sighting_ids):
        if pick["x_post_id"]:
            erased.x_posts.append(pick["x_post_id"])
        elif pick["status"] == "posting":
            # Started and never confirmed: it may be on X. The pick goes with
            # the sighting, so what it knew is kept as a takedown for a person
            # to check (app/social.py). A daily run still waiting on X settles
            # it either way when it hears back.
            erased.unconfirmed_posts.append((pick["id"], pick["caption"]))
            logger.warning("erased a sighting whose post to X is unconfirmed: pick=%s", pick["id"])
    return individuals, partners


async def _after(conn, individuals: list[UUID], partners: list[UUID]) -> None:
    # The identity stays, with whatever sightings are left (Q3). Its cached
    # dates must not keep a deleted sighting's time.
    if individuals:
        await conn.execute(
            """UPDATE individuals i SET
                   first_seen_at = (SELECT min(captured_at) FROM sightings WHERE individual_id=i.id),
                   last_seen_at = (SELECT max(captured_at) FROM sightings WHERE individual_id=i.id),
                   updated_at = now()
               WHERE id=ANY($1::uuid[])""", individuals)
    # A sighting whose only open proposal was with a deleted one has nothing
    # left to decide, as when the last proposal is answered (routes/match.py).
    if partners:
        await conn.execute(
            """UPDATE sightings s SET match_status='unmatched'
               WHERE id=ANY($1::uuid[]) AND match_status='proposed'
                 AND NOT EXISTS (SELECT 1 FROM match_proposals
                                 WHERE sighting_id=s.id AND status='pending')""", partners)


async def _owe(conn, erased: Erased, reason: str) -> None:
    for key, kind in erased.media:
        await conn.execute(
            "INSERT INTO media_purges (s3_key, reason, kind) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
            key, reason, kind)
    for post_id in erased.x_posts:
        await conn.execute(
            "INSERT INTO x_post_purges (id, x_post_id, reason) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
            uuid7(), post_id, reason)
    for pick_id, caption in erased.unconfirmed_posts:
        await conn.execute(
            "INSERT INTO x_post_purges (id, pick_id, caption, reason) VALUES ($1,$2,$3,$4)",
            uuid7(), pick_id, caption, reason + ":unconfirmed")


async def _erase_capture_locked(conn, capture, reason: str) -> Erased:
    cid = capture["id"]
    erased = Erased("capture", cid)
    sightings = [r["id"] for r in await conn.fetch("SELECT id FROM sightings WHERE capture_id=$1", cid)]
    prefixes = _prefixes(cid, sightings)
    # Every key a row names, beyond the prefixes: one stored elsewhere would
    # otherwise be left behind with nothing pointing at it.
    named = _photo_keys(r["s3_key"] for r in await conn.fetch(
        "SELECT s3_key FROM photos WHERE capture_id=$1 OR sighting_id=ANY($2::uuid[])", cid, sightings))
    named += [r["s3_key"] for r in await conn.fetch(
        "SELECT s3_key FROM voice_notes WHERE capture_id=$1 OR sighting_id=ANY($2::uuid[])", cid, sightings)]
    named += [capture["clip_s3_key"], capture["audio_s3_key"]]
    named += [k for r in await conn.fetch(
        "SELECT clip_s3_key, audio_s3_key FROM sightings WHERE id=ANY($1::uuid[])", sightings) for k in r]
    erased.media = [(p, "prefix") for p in prefixes] + sorted(
        {(k, "key") for k in named if k and not any(k.startswith(p) for p in prefixes)})

    individuals, partners = await _before(conn, sightings, erased)
    # Sightings first: their capture key has no ON DELETE rule (REFERENCES).
    await conn.execute("DELETE FROM sightings WHERE id=ANY($1::uuid[])", sightings)
    await conn.execute("DELETE FROM captures WHERE id=$1", cid)
    await _after(conn, individuals, partners)
    await _owe(conn, erased, reason)
    return erased


async def _erase_animal_locked(conn, capture, sighting_id: UUID, reason: str) -> Erased:
    """One animal of an upload that has others in it."""
    erased = Erased("sighting", sighting_id)
    instances = await conn.fetch(
        "SELECT id, evidence_photo_id FROM animal_instances WHERE sighting_id=$1", sighting_id)
    instance_ids = [r["id"] for r in instances]
    photos = await conn.fetch(
        "SELECT id, s3_key FROM photos WHERE sighting_id=$1 OR id=ANY($2::uuid[])",
        sighting_id, [r["evidence_photo_id"] for r in instances])
    notes = await conn.fetch("SELECT id, s3_key FROM voice_notes WHERE sighting_id=$1", sighting_id)
    own = await conn.fetchrow("SELECT clip_s3_key, audio_s3_key FROM sightings WHERE id=$1", sighting_id)
    keys = _photo_keys(r["s3_key"] for r in photos) + [r["s3_key"] for r in notes] + [k for k in own if k]
    erased.media = sorted({(k, "key") for k in keys})

    individuals, partners = await _before(conn, [sighting_id], erased)
    # Suggestions read from a note about this animal: its words are going, and
    # so is the animal they were about.
    await conn.execute(
        """DELETE FROM detail_suggestions WHERE transcript_id IN
               (SELECT id FROM voice_transcripts WHERE note_id=ANY($1::uuid[]))""",
        [r["id"] for r in notes])
    await conn.execute("DELETE FROM animal_instances WHERE id=ANY($1::uuid[])", instance_ids)
    # A model already asked about this upload was shown this animal under a
    # letter; on its answer, the letters would be read against the animals
    # left, and this one's details pinned on another. Its claim is reset, so
    # the answer is refused (409 to a worker, dropped in-process) and the note
    # is read again about the animals that remain.
    await conn.execute(
        """UPDATE voice_transcripts SET suggest_status='pending', suggest_lease_until=NULL,
               suggest_lease_token_hash=NULL, suggest_run_after=now()
           WHERE capture_id=$1 AND suggest_status='running'""", capture["id"])
    await conn.execute(
        """UPDATE transcript_backfill b SET status='pending', lease_until=NULL, lease_token_hash=NULL,
               run_after=now()
           FROM voice_transcripts t
           WHERE t.id=b.transcript_id AND t.capture_id=$1 AND b.status='running'""", capture["id"])
    await conn.execute("DELETE FROM photos WHERE id=ANY($1::uuid[])", [r["id"] for r in photos])
    await conn.execute("DELETE FROM sightings WHERE id=$1", sighting_id)
    # The saved review must still cover exactly the boxes that exist, or the
    # next edit of the other animals' details is refused (captures.validate_groups).
    groups = capture["review_groups"]
    groups = json.loads(groups) if isinstance(groups, str) else (groups or [])
    gone = {str(i) for i in instance_ids}
    kept = [g for g in groups if not gone & {str(i) for i in g.get("instance_ids", [])}]
    await conn.execute(
        "UPDATE captures SET review_groups=$2::jsonb, revision=revision+1, updated_at=now() WHERE id=$1",
        capture["id"], json.dumps(kept))
    await _after(conn, individuals, partners)
    await _owe(conn, erased, reason)
    return erased


async def erase_capture(conn, capture_id: UUID, *, reason: str, owner: UUID | None = None) -> Erased | None:
    """Delete an upload and everything in it. None if there is no such capture
    (or it is not `owner`'s). Caller holds a transaction."""
    capture = await _lock(conn, capture_id)
    if capture is None or (owner is not None and capture["observer_id"] != owner):
        return None
    return await _erase_capture_locked(conn, capture, reason)


async def erase_sighting(conn, sighting_id: UUID, *, reason: str, owner: UUID | None = None) -> Erased | None:
    """Delete one animal; the whole upload if it was the last one in it.
    None if there is no such sighting (or it is not `owner`'s). Caller holds a
    transaction."""
    capture_id = await conn.fetchval("SELECT capture_id FROM sightings WHERE id=$1", sighting_id)
    if capture_id is None:
        return None
    capture = await _lock(conn, capture_id)
    row = await conn.fetchrow("SELECT observer_id, capture_id FROM sightings WHERE id=$1", sighting_id)
    if capture is None or row is None or row["capture_id"] != capture_id:
        return None       # gone, or moved, while we waited for the locks
    if owner is not None and row["observer_id"] != owner:
        return None
    others = await conn.fetchval(
        "SELECT count(*) FROM sightings WHERE capture_id=$1 AND id<>$2", capture_id, sighting_id)
    if not others:
        return await _erase_capture_locked(conn, capture, reason)
    return await _erase_animal_locked(conn, capture, sighting_id, reason)


def ledger_key(erased: Erased, at: datetime) -> str:
    return f"erasures/{at:%Y-%m-%d}/{erased.kind}-{erased.id}.json"


async def write_ledger(storage, erased: Erased, at: datetime | None = None) -> None:
    """An id-only record that this was erased, kept outside the database.

    A database restored from a backup taken before the erasure would bring it
    back; scripts/replay_erasures.py reads these and erases it again. Off
    unless `erasure_ledger_enabled`. Written before the erasure commits: if
    the store refuses, nothing is deleted and the caller can try again, rather
    than deleting something a restore would silently resurrect."""
    at = at or datetime.now(timezone.utc)
    body = json.dumps({"kind": erased.kind, "id": str(erased.id), "at": at.isoformat()})
    await storage.put(ledger_key(erased, at), body.encode(), "application/json")


async def erase(pool, storage, kind: str, target: UUID, *, reason: str,
                owner: UUID | None = None, ledger: bool = True) -> Erased | None:
    """Erase one capture or sighting in its own transaction, then ask the
    store (and X) to delete what it owes. Retries a deadlock: a worker
    finishing on this capture at that moment is the one case that can."""
    from app.media_purges import purge_later
    from app.social import purge_x_later
    fn = {"capture": erase_capture, "sighting": erase_sighting}[kind]
    for attempt in range(DEADLOCK_RETRIES):
        try:
            async with pool.acquire() as conn, conn.transaction():
                erased = await fn(conn, target, reason=reason, owner=owner)
                if erased is not None and ledger and settings.erasure_ledger_enabled:
                    try:
                        await write_ledger(storage, erased)
                    except Exception as exc:
                        logger.warning("erasure ledger refused %s=%s (%s); nothing erased",
                                       kind, target, type(exc).__name__)
                        raise LedgerUnavailable() from exc
            break
        except asyncpg.exceptions.DeadlockDetectedError:
            if attempt == DEADLOCK_RETRIES - 1:
                raise
            await asyncio.sleep(0.05 * (attempt + 1))
    if erased is None:
        return None
    logger.info("erased %s=%s reason=%s media=%d x_posts=%d",
                erased.kind, erased.id, reason, len(erased.media), len(erased.x_posts))
    for key, _ in erased.media:
        purge_later(pool, storage, key)
    for post_id in erased.x_posts:
        purge_x_later(pool, post_id)
    return erased
