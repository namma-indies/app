"""Existing transcripts queued for today's model pass (#176).

Transcripts from before #153 have no English line, and some never got
suggestions (skipped, or failed in the #108 era). `transcript_backfill` is
their queue: one row per transcript, with `mode` 'full' (English and
suggestions, for one whose pass never completed and has none) or
'translate' (English only; any suggestions in the answer are dropped). It is
a queue of its own, apart from `voice_transcripts.suggest_*`, so nothing it
holds makes a note look "being read" in the app, and the GPU worker drains it
last, at TRANSCRIPT_BACKFILL_PER_HOUR. It goes with its transcript: a note
transcribed again (redo, scripts/retranscribe_misdetected.py) is not read twice.

Revision ID: 0030_transcript_backfill
Revises: 0029_language_diagnostics
Create Date: 2026-10-30

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0030_transcript_backfill"
down_revision: Union[str, None] = "0029_language_diagnostics"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE transcript_backfill (
            id uuid PRIMARY KEY,
            transcript_id uuid NOT NULL UNIQUE REFERENCES voice_transcripts(id) ON DELETE CASCADE,
            mode text NOT NULL CHECK (mode IN ('full', 'translate')),
            status text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'done', 'failed', 'skipped')),
            attempts int NOT NULL DEFAULT 0,
            run_after timestamptz NOT NULL DEFAULT now(),
            first_claimed_at timestamptz,
            lease_until timestamptz,
            lease_token_hash text,
            error text,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_transcript_backfill_queue ON transcript_backfill (created_at, id)
            WHERE status IN ('pending', 'running');
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS transcript_backfill;")
