"""How whisper settled a transcript's language (#173).

`voice_transcripts.language_diagnostics`: what it detected, what the sound
was transcribed in, and its three likeliest languages with their
probabilities, as the GPU worker reports them. Codes and numbers only, never
words. NULL for transcripts from before, and from a worker that predates it.
Only for working out misdetections (English heard as Malay, say): nothing
shows it.

Revision ID: 0029_language_diagnostics
Revises: 0028_redo_language
Create Date: 2026-10-30

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0029_language_diagnostics"
down_revision: Union[str, None] = "0028_redo_language"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE voice_transcripts ADD COLUMN language_diagnostics jsonb;")


def downgrade() -> None:
    op.execute("ALTER TABLE voice_transcripts DROP COLUMN IF EXISTS language_diagnostics;")
