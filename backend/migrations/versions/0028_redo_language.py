"""How many times a sound was transcribed again in another language (#169).

"Wrong language? Redo in <yours>" transcribes one recording again, once:
the count lives on its transcription job, which a redo keeps (the transcript
itself is replaced). 0 for every job before this.

Revision ID: 0028_redo_language
Revises: 0027_note_language
Create Date: 2026-10-30

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0028_redo_language"
down_revision: Union[str, None] = "0027_note_language"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE transcription_jobs ADD COLUMN redo_count int NOT NULL DEFAULT 0;")


def downgrade() -> None:
    op.execute("ALTER TABLE transcription_jobs DROP COLUMN IF EXISTS redo_count;")
