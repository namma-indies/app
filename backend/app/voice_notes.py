"""What was heard on a capture: its upload's note and clip, and notes added later (#156).

One reader for the Journal (/dex) and the details sheet (/capture/{id}), so
both list the same transcripts in the same order: the upload's note, its
clip, then notes added later, oldest first. A note added later also has a
state of its own, for the viewer to show while it is being worked on:

- "transcribing": queued for, or on, the GPU worker's whisper;
- "reading": transcribed, the model still reading it for details;
- "done": transcribed and read (its transcript is in the list);
- "no_speech": nothing was said in it;
- "failed": it could not be transcribed.

A transcript in another language than the one its observer speaks their notes
in carries `redo_language` (#169): that language, while one redo is still
allowed and the redo is switched on. An upload's own note or clip being
transcribed again is listed in "relistening" with a state: "listening" until its
new transcript comes, or "failed" / "no_speech" if it never will; once it has
come, the transcript says `reading` until the model has read it for details.
"""
from uuid import UUID

from app.config import settings
from app.note_language import OFFERED
from app.suggestions import suggestions_active
from app.transcription_jobs import PIPELINE_VERSION, transcription_enabled


def redo_allowed() -> bool:
    """Whether "Redo in <language>" can work now (app/routes/voice_notes.py)."""
    return settings.language_hints_enabled and transcription_enabled()


def note_state(job_status: str | None, outcome: str | None, suggest_status: str | None) -> str:
    if outcome is not None:
        if outcome != "transcribed":
            return "no_speech"
        # With nothing to read it, a pending pass would never end: it is done.
        if suggest_status in ("pending", "running") and suggestions_active():
            return "reading"
        return "done"
    return "transcribing" if job_status in ("pending", "running") else "failed"


async def heard(conn, storage, capture_ids) -> dict[UUID, dict[str, list[dict]]]:
    """Per capture: {"transcripts": [...], "notes": [...], "relistening": [...]}.

    Each transcript and later note carries `sighting_id`, the animal a later
    note was recorded about (None for the upload's sounds and a note added to
    the whole capture), for `for_sighting` to filter on.
    """
    capture_ids = list(capture_ids)
    out: dict[UUID, dict[str, list[dict]]] = {
        c: {"transcripts": [], "notes": [], "relistening": []} for c in capture_ids}
    if not capture_ids:
        return out
    notes = await conn.fetch(
        """SELECT n.id, n.capture_id, n.sighting_id, n.created_at, n.s3_key,
                  j.status AS job_status, t.outcome, t.suggest_status
           FROM voice_notes n
           LEFT JOIN transcription_jobs j ON j.note_id = n.id AND j.pipeline_version = $2
           LEFT JOIN voice_transcripts t ON t.note_id = n.id AND t.pipeline_version = $2
           WHERE n.capture_id = ANY($1::uuid[]) ORDER BY n.created_at, n.id""",
        capture_ids, PIPELINE_VERSION)
    urls = await storage.urls([n["s3_key"] for n in notes]) if notes else []
    by_note = {}
    for n, url in zip(notes, urls):
        note = {"id": n["id"], "sighting_id": n["sighting_id"], "added_at": n["created_at"],
                "audio_url": url, "state": note_state(n["job_status"], n["outcome"], n["suggest_status"])}
        by_note[n["id"]] = note
        out[n["capture_id"]]["notes"].append(note)
    # Read before the transcripts (#170 review): if a redo's new transcript lands
    # between the two reads, the answer shows it still listening beside the new
    # words, and the viewer looks once more, rather than neither, and stops.
    # The upload's own note or clip transcribed again (a later note says so
    # through its own state): still listening, or how it ended without words,
    # since the old ones are gone.
    for j in await conn.fetch(
            """SELECT j.capture_id, j.source, j.status, t.outcome FROM transcription_jobs j
               LEFT JOIN voice_transcripts t ON t.capture_id = j.capture_id AND t.source = j.source
                    AND t.note_id IS NULL AND t.pipeline_version = $2
               WHERE j.capture_id = ANY($1::uuid[]) AND j.pipeline_version = $2 AND j.note_id IS NULL
                 AND j.redo_count > 0 AND (t.outcome IS NULL OR t.outcome <> 'transcribed')
               ORDER BY j.source DESC""", capture_ids, PIPELINE_VERSION):
        state = ("no_speech" if j["outcome"] is not None
                 else "listening" if j["status"] in ("pending", "running") else "failed")
        out[j["capture_id"]]["relistening"].append({"source": j["source"], "state": state, "sighting_id": None})
    redo = redo_allowed()
    for t in await conn.fetch(
            """SELECT t.id, t.capture_id, t.source, t.language, t.text, t.english, t.note_id,
                      t.suggest_status, j.redo_count, o.note_language AS usual
               FROM voice_transcripts t LEFT JOIN voice_notes n ON n.id = t.note_id
               JOIN captures c ON c.id = t.capture_id
               LEFT JOIN observers o ON o.id = c.observer_id
               LEFT JOIN transcription_jobs j ON j.capture_id = t.capture_id AND j.source = t.source
                    AND j.note_id IS NOT DISTINCT FROM t.note_id AND j.pipeline_version = t.pipeline_version
               WHERE t.capture_id = ANY($1::uuid[]) AND t.pipeline_version = $2
                 AND t.outcome = 'transcribed'
               ORDER BY t.note_id IS NOT NULL, t.source DESC, n.created_at, n.id""",
            capture_ids, PIPELINE_VERSION):
        note = by_note.get(t["note_id"])
        offer = (redo and t["usual"] in OFFERED and t["usual"] != t["language"]
                 and t["redo_count"] == 0)
        # A redone upload note or clip is still being read for details (a later
        # note says so through its own state).
        reading = (t["note_id"] is None and (t["redo_count"] or 0) > 0 and suggestions_active()
                   and t["suggest_status"] in ("pending", "running"))
        out[t["capture_id"]]["transcripts"].append({
            "id": t["id"], "redo_language": t["usual"] if offer else None, "reading": reading,
            "source": t["source"], "language": t["language"], "text": t["text"],
            "english": t["english"], "note_id": t["note_id"],
            "added_at": note["added_at"] if note else None,
            "audio_url": note["audio_url"] if note else None,
            "sighting_id": note["sighting_id"] if note else None,
        })
    return out


def for_sighting(found: dict[str, list[dict]], sighting_id: UUID | None) -> dict[str, list[dict]]:
    """What one sighting shows: everything but notes recorded about another
    animal of the same capture. `sighting_id` None (the details sheet) keeps all.
    A later note keeps its `sighting_id`, so the app can count those that
    weigh against each one's limit."""
    keep = (lambda item: True) if sighting_id is None else (
        lambda item: item["sighting_id"] in (None, sighting_id))
    return {"transcripts": [{key: v for key, v in t.items() if key != "sighting_id"}
                            for t in found["transcripts"] if keep(t)],
            "notes": [n for n in found["notes"] if keep(n)],
            "relistening": [{"source": r["source"], "state": r["state"]} for r in found["relistening"]]}
