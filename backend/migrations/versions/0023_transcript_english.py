"""An English translation kept beside a non-English transcript (#152).

Written by the same model pass that reads the transcript for suggestions
(app/suggestions.py), and only shown: no suggestion is made or checked from
it. NULL for an English transcript, one too long to translate, one whose pass
has not run, and every transcript made before this. `english_model` records
which model wrote it, as `detail_suggestions.model` does for suggestions.

Revision ID: 0023_transcript_english
Revises: 0022_coarse_point
Create Date: 2026-10-30

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0023_transcript_english"
down_revision: Union[str, None] = "0022_coarse_point"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE voice_transcripts ADD COLUMN english text, ADD COLUMN english_model text;")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE voice_transcripts DROP COLUMN IF EXISTS english_model, "
        "DROP COLUMN IF EXISTS english;")
