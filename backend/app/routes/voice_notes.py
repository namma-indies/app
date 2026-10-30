"""Add a voice note to your own sighting or capture after it was uploaded (#156).

The note is the same as one recorded at upload, and goes through the same
steps: stored under the row's own media prefix, transcribed on the GPU
worker, translated and read for suggestions (app/suggestions.py). Only the
observer who logged it can add one.

From a sighting (`POST /sighting/{id}/voice-note`) the note is about that
animal, which on a multi-animal capture means it is read for that animal
alone. From a capture's details sheet (`POST /capture/{id}/voice-note`) it is
about the whole capture, like the upload's own note.

A save is safe to retry: the app sends the recording's own `client_token`,
and a retry whose first answer was lost gets the note already made back
(202, `duplicate`), before any limit is applied, rather than a second copy.

`DELETE /voice-note/{id}` removes a note added later, for good: its audio,
its transcript and the suggestions from it still open. Details already
filled in from it stay, as they are the observer's own now; so does each
answered suggestion, as the record of how the model did, but without the
words it quoted, which were the recording's (0025).
The rows go at once, in one transaction that also records the audio's key in
`media_purges`; the audio is deleted from the store after the response
(app/media_purges.py). The observer never waits on the store: if it refuses
(production's credentials may lack s3:DeleteObject) or does not answer, the
key stays recorded for scripts/purge_media.py to finish.

Limits: the recorder stops at 60 s (the server never decodes audio, as at
upload); the upload's byte cap; `later_voice_notes_max` per sighting or
capture; `rl_voice_note_*` per person.
"""
from uuid import UUID, uuid5

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from app.auth.deps import require_observer
from app.config import settings
from app.deps import get_conn, get_storage
from app.ids import uuid7
from app.media_purges import lock_key, purge_later
from app.note_language import check_choice, hint_for
from app.ratelimit import check, voice_note_limit
from app.routes.capture import owned
from app.routes.sighting import read_voice_note
from app.transcription_jobs import enqueue_transcription, transcription_enabled

router = APIRouter()
# Names a note after its recording's client_token (see `_note_id`).
_NOTE_NAMESPACE = UUID("4f1c2a8e-7d3b-5e9a-b6c4-0a1d2e3f4b5c")


def _ready(observer_id: UUID) -> None:
    # Without a worker transcribing, a note would only ever say "transcribing".
    if not transcription_enabled():
        raise HTTPException(503, "voice notes cannot be added right now")
    check(f"voice-note:obs:{observer_id}", voice_note_limit())


async def _count(conn, capture_id: UUID, sighting_id: UUID | None) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM voice_notes WHERE capture_id=$1 AND sighting_id IS NOT DISTINCT FROM $2",
        capture_id, sighting_id)


async def _already(conn, observer_id: UUID, client_token: str | None) -> JSONResponse | None:
    """The note this recording already made, if a save of it got through before."""
    if not client_token:
        return None
    found = await conn.fetchval(
        "SELECT id FROM voice_notes WHERE observer_id=$1 AND client_token=$2", observer_id, client_token)
    return None if found is None else JSONResponse(
        {"note_id": str(found), "duplicate": True}, status_code=202)


def _note_id(observer_id: UUID, client_token: str | None) -> UUID:
    """A recording's note id, and so its storage key. Named after its token
    when it has one, so two saves of it that overlap write the one object
    and the second finds the first's row, leaving no stray copy of the audio."""
    return uuid5(_NOTE_NAMESPACE, f"{observer_id}:{client_token}") if client_token else uuid7()


async def _add(conn, storage, observer_id: UUID, capture_id: UUID, legacy: bool,
               sighting_id: UUID | None, audio: UploadFile, client_token: str | None,
               note_language: str | None = None, usual_language: str | None = None) -> JSONResponse:
    raw, ext, content_type = await read_voice_note(audio)
    chosen_language = check_choice(note_language)
    usual_sent = check_choice(usual_language)
    if (done := await _already(conn, observer_id, client_token)) is not None:
        return done
    if await _count(conn, capture_id, sighting_id) >= settings.later_voice_notes_max:
        raise HTTPException(409, "this sighting already has as many added notes as it can take")
    note_id = _note_id(observer_id, client_token)
    # Beside the row's other media, as the upload's note is (0019).
    prefix = f"sightings/{capture_id}" if legacy else f"captures/{capture_id}"
    key = f"{prefix}/notes/{note_id}.{ext}"
    async with conn.transaction():
        # Serialises two notes saved at once, so the limit holds.
        await conn.execute("SELECT 1 FROM captures WHERE id=$1 FOR UPDATE", capture_id)
        # The same recording saved twice at once: the second finds the first.
        if (done := await _already(conn, observer_id, client_token)) is not None:
            return done
        if await _count(conn, capture_id, sighting_id) >= settings.later_voice_notes_max:
            raise HTTPException(409, "this sighting already has as many added notes as it can take")
        # The key may be owed a purge (this recording, saved before, then
        # deleted): take it back under the purge's own lock before writing it.
        await lock_key(conn, key)
        await conn.execute("DELETE FROM media_purges WHERE s3_key=$1", key)
        try:
            await storage.put(key, raw, content_type)
        except Exception:
            raise HTTPException(503, "voice note storage unavailable; try again")
        await conn.execute(
            """INSERT INTO voice_notes (id, capture_id, sighting_id, observer_id, s3_key, content_type, bytes,
                                        client_token)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
            note_id, capture_id, sighting_id, observer_id, key, content_type, len(raw), client_token)
        hint, forced = await hint_for(conn, observer_id, chosen_language, usual_sent)
        await enqueue_transcription(conn, capture_id, "note", key, note_id, hint=hint, forced=forced)
    return JSONResponse({"note_id": str(note_id), "state": "transcribing"}, status_code=202)


@router.post("/sighting/{sighting_id}/voice-note", status_code=202)
async def add_to_sighting(sighting_id: UUID, audio: UploadFile = File(...),
                          client_token: str | None = Form(None, max_length=200),
                          note_language: str | None = Form(None, max_length=8),
                          usual_language: str | None = Form(None, max_length=8),
                          observer_id: UUID = Depends(require_observer), conn=Depends(get_conn),
                          storage=Depends(get_storage)):
    _ready(observer_id)
    row = await conn.fetchrow(
        """SELECT s.capture_id, c.processing_state FROM sightings s JOIN captures c ON c.id = s.capture_id
           WHERE s.id=$1 AND s.observer_id=$2""", sighting_id, observer_id)
    if row is None:
        raise HTTPException(404, "no such sighting")
    return await _add(conn, storage, observer_id, row["capture_id"], row["processing_state"] == "legacy",
                      sighting_id, audio, client_token, note_language, usual_language)


@router.post("/capture/{capture_id}/voice-note", status_code=202)
async def add_to_capture(capture_id: UUID, audio: UploadFile = File(...),
                         client_token: str | None = Form(None, max_length=200),
                         note_language: str | None = Form(None, max_length=8),
                         usual_language: str | None = Form(None, max_length=8),
                         observer_id: UUID = Depends(require_observer), conn=Depends(get_conn),
                         storage=Depends(get_storage)):
    _ready(observer_id)
    capture = await owned(conn, capture_id, observer_id)
    # A /sighting upload's stand-in capture has no sheet; its note goes on the sighting.
    if capture["processing_state"] == "legacy":
        raise HTTPException(404, "no such capture")
    return await _add(conn, storage, observer_id, capture_id, False, None, audio, client_token, note_language,
                      usual_language)


@router.delete("/voice-note/{note_id}", status_code=204)
async def delete_note(note_id: UUID, request: Request, observer_id: UUID = Depends(require_observer),
                      conn=Depends(get_conn), storage=Depends(get_storage)):
    async with conn.transaction():
        # Locks in the order a transcription completing takes them (its job,
        # then the note its transcript points at), so the two never deadlock.
        await conn.execute("SELECT 1 FROM transcription_jobs WHERE note_id=$1 FOR UPDATE", note_id)
        key = await conn.fetchval(
            "SELECT s3_key FROM voice_notes WHERE id=$1 AND observer_id=$2 FOR UPDATE", note_id, observer_id)
        if key is None:
            raise HTTPException(404, "no such voice note")
        # Fence the model first: suggestions being stored for this note finish
        # before the cleanup below sees them, and none can be stored after it.
        await conn.execute("SELECT 1 FROM voice_transcripts WHERE note_id=$1 FOR UPDATE", note_id)
        heard = "SELECT id FROM voice_transcripts WHERE note_id=$1"
        await conn.execute(f"DELETE FROM detail_suggestions WHERE status='open' AND transcript_id IN ({heard})", note_id)
        await conn.execute(f"UPDATE detail_suggestions SET quote='' WHERE transcript_id IN ({heard})", note_id)
        # Its transcription job and transcript go with it (0024); the answered
        # suggestions above are let go of their transcript (0025).
        await conn.execute("DELETE FROM voice_notes WHERE id=$1 AND observer_id=$2", note_id, observer_id)
        # The audio is owed a delete from here on, with the rows: if the store
        # refuses, or this process stops before asking, the key is on record.
        await conn.execute(
            "INSERT INTO media_purges (s3_key, reason) VALUES ($1, 'voice_note_deleted') ON CONFLICT DO NOTHING", key)
    # Gone for its observer now. The store is asked after the response, bounded
    # in time; what it will not delete waits for scripts/purge_media.py.
    purge_later(request.app.state.pool, storage, key)
    return Response(status_code=204)
