"""The language someone speaks their voice notes in, as a hint to whisper (#167).

`observers.note_language` is asked once, the first time someone records a
note: one of the languages offered, or 'auto' (mixed: let the app decide).
NULL means not asked yet, or skipped, and the app asks again.

Each transcription job carries the hint for its sound:
`transcription_jobs.language_hint` is the language to transcribe in, and
`language_forced` says whether that is the person's choice for this one note
(transcribe in exactly that) or only their usual language (a strong hint a
very confident detection of another language may override). NULL and false
for every job made before this, for old builds and for 'auto': today's
behaviour. Only the GPU worker reads them, and one that predates them ignores
them (gpu_worker/client.py reads the job's fields by name).

Revision ID: 0027_note_language
Revises: 0026_photo_descriptions
Create Date: 2026-10-30

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0027_note_language"
down_revision: Union[str, None] = "0026_photo_descriptions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# English and the Indian languages whisper knows (gpu_worker/transcribe.ALLOWED).
_HINTS = "'en','hi','kn','ta','te','ml','mr','bn','gu','pa','ur'"


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE observers ADD COLUMN note_language text
            CHECK (note_language IN ({_HINTS}, 'auto'));
        ALTER TABLE transcription_jobs
            ADD COLUMN language_hint text CHECK (language_hint IN ({_HINTS})),
            ADD COLUMN language_forced boolean NOT NULL DEFAULT false;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE transcription_jobs DROP COLUMN IF EXISTS language_forced,
            DROP COLUMN IF EXISTS language_hint;
        ALTER TABLE observers DROP COLUMN IF EXISTS note_language;
        """
    )
