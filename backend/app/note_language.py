"""The language someone speaks their voice notes in (#167).

Asked once, the first time they record a note, and kept on the observer. It
becomes a hint on each transcription job (`hint_for`): the GPU worker
transcribes in it unless it is very sure the note is in another of the
languages it allows. A choice made for one note on the recorder is stronger:
that note is transcribed in exactly that language. 'auto' (mixed, let the
app decide) and no answer at all mean today's behaviour: no hint.

The hint only takes effect on a worker that reads it; one that predates it
ignores the job fields it does not know.
"""
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.deps import require_observer
from app.deps import get_conn

router = APIRouter()

# The languages the app offers, in the order it offers them.
OFFERED = ("en", "kn", "hi", "ta", "te", "ml")
Preference = Literal["en", "kn", "hi", "ta", "te", "ml", "auto"]


def check_choice(value: str | None) -> str | None:
    """A per-note choice from an upload form, or a 422. Anything but a
    non-empty string (absent, or a route called directly) is no choice."""
    if not isinstance(value, str) or value == "":
        return None
    if value not in (*OFFERED, "auto"):
        raise HTTPException(422, "not a language notes can be in")
    return value


async def hint_for(conn, observer_id: UUID, chosen: str | None,
                   usual_sent: str | None = None) -> tuple[str | None, bool]:
    """(language_hint, language_forced) for a sound this observer recorded.

    `chosen` is the language picked for this one note, if any. `usual_sent` is
    the usual language the app showed when the note was recorded: it travels
    with the upload so an answer whose own save failed (the phone offline, say)
    still reaches the job, and is kept on the observer if nothing is yet. What
    is kept wins otherwise.
    """
    if chosen is not None:
        return (None, False) if chosen == "auto" else (chosen, True)
    usual = await conn.fetchval("SELECT note_language FROM observers WHERE id=$1", observer_id)
    if usual is None and usual_sent is not None:
        await conn.execute("UPDATE observers SET note_language=$2, updated_at=now() "
                           "WHERE id=$1 AND note_language IS NULL", observer_id, usual_sent)
        usual = usual_sent
    return (usual, False) if usual in OFFERED else (None, False)


class NoteLanguage(BaseModel):
    language: Preference


@router.put("/me/note-language")
async def set_note_language(body: NoteLanguage, observer_id: UUID = Depends(require_observer),
                            conn=Depends(get_conn)):
    await conn.execute("UPDATE observers SET note_language=$2, updated_at=now() WHERE id=$1",
                       observer_id, body.language)
    return {"note_language": body.language}
