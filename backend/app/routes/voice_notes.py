"""Add a voice note to your own sighting or capture after it was uploaded (#156).

The note is the same as one recorded at upload, and goes through the same
steps: stored under the row's own media prefix, transcribed on the GPU
worker, translated and read for suggestions (app/suggestions.py). Only the
observer who logged it can add one.

From a sighting (`POST /sighting/{id}/voice-note`) the note is about that
animal, which on a multi-animal capture means it is read for that animal
alone. From a capture's details sheet (`POST /capture/{id}/voice-note`) it is
about the whole capture, like the upload's own note.

Limits: the recorder stops at 60 s (the server never decodes audio, as at
upload); the upload's byte cap; `later_voice_notes_max` per sighting or
capture; `rl_voice_note_*` per person.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from app.auth.deps import require_observer
from app.config import settings
from app.deps import get_conn, get_storage
from app.ids import uuid7
from app.ratelimit import check, voice_note_limit
from app.routes.capture import owned
from app.routes.sighting import read_voice_note
from app.transcription_jobs import enqueue_transcription, transcription_enabled

router = APIRouter()


def _ready(observer_id: UUID) -> None:
    # Without a worker transcribing, a note would only ever say "transcribing".
    if not transcription_enabled():
        raise HTTPException(503, "voice notes cannot be added right now")
    check(f"voice-note:obs:{observer_id}", voice_note_limit())


async def _count(conn, capture_id: UUID, sighting_id: UUID | None) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM voice_notes WHERE capture_id=$1 AND sighting_id IS NOT DISTINCT FROM $2",
        capture_id, sighting_id)


async def _add(conn, storage, observer_id: UUID, capture_id: UUID, legacy: bool,
               sighting_id: UUID | None, audio: UploadFile) -> JSONResponse:
    raw, ext, content_type = await read_voice_note(audio)
    if await _count(conn, capture_id, sighting_id) >= settings.later_voice_notes_max:
        raise HTTPException(409, "this sighting already has as many added notes as it can take")
    note_id = uuid7()
    # Beside the row's other media, as the upload's note is (0019).
    prefix = f"sightings/{capture_id}" if legacy else f"captures/{capture_id}"
    key = f"{prefix}/notes/{note_id}.{ext}"
    try:
        await storage.put(key, raw, content_type)
    except Exception:
        raise HTTPException(503, "voice note storage unavailable; try again")
    async with conn.transaction():
        # Serialises two notes saved at once, so the limit holds.
        await conn.execute("SELECT 1 FROM captures WHERE id=$1 FOR UPDATE", capture_id)
        if await _count(conn, capture_id, sighting_id) >= settings.later_voice_notes_max:
            raise HTTPException(409, "this sighting already has as many added notes as it can take")
        await conn.execute(
            """INSERT INTO voice_notes (id, capture_id, sighting_id, observer_id, s3_key, content_type, bytes)
               VALUES ($1,$2,$3,$4,$5,$6,$7)""",
            note_id, capture_id, sighting_id, observer_id, key, content_type, len(raw))
        await enqueue_transcription(conn, capture_id, "note", key, note_id)
    return JSONResponse({"note_id": str(note_id), "state": "transcribing"}, status_code=202)


@router.post("/sighting/{sighting_id}/voice-note", status_code=202)
async def add_to_sighting(sighting_id: UUID, audio: UploadFile = File(...),
                          observer_id: UUID = Depends(require_observer), conn=Depends(get_conn),
                          storage=Depends(get_storage)):
    _ready(observer_id)
    row = await conn.fetchrow(
        """SELECT s.capture_id, c.processing_state FROM sightings s JOIN captures c ON c.id = s.capture_id
           WHERE s.id=$1 AND s.observer_id=$2""", sighting_id, observer_id)
    if row is None:
        raise HTTPException(404, "no such sighting")
    return await _add(conn, storage, observer_id, row["capture_id"], row["processing_state"] == "legacy",
                      sighting_id, audio)


@router.post("/capture/{capture_id}/voice-note", status_code=202)
async def add_to_capture(capture_id: UUID, audio: UploadFile = File(...),
                         observer_id: UUID = Depends(require_observer), conn=Depends(get_conn),
                         storage=Depends(get_storage)):
    _ready(observer_id)
    capture = await owned(conn, capture_id, observer_id)
    # A /sighting upload's stand-in capture has no sheet; its note goes on the sighting.
    if capture["processing_state"] == "legacy":
        raise HTTPException(404, "no such capture")
    return await _add(conn, storage, observer_id, capture_id, False, None, audio)
