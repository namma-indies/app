"""Suggested animal details read from a transcript, decided by the observer.

A language model reads what the observer said and proposes values for the
details they would otherwise tap in: sex, ear notch, condition, a known name.
It proposes, never decides. Each suggestion is a row of its own carrying the
words that prompted it, and nothing in `sightings.attrs` changes until the
observer picks an option. Their answer is kept beside the suggestion
(`accepted`, `corrected` to another value, or `dismissed`), so how often the
model is right can be measured before it is trusted with more.

On a multi-animal capture the model also sees one photo of each animal it
found and must say which animal a detail is about; `instance_id` is the
evidence it was shown for that animal, so the review sheet can put the
suggestion under whichever group that box ends up in. NULL for a /sighting
upload, which has only the one animal.

`voice_transcripts.suggest_*` is the small work queue for the model call: one
pass per transcript, leased by timestamp so several API processes can share it
without holding a database connection across a network call. When the model
runs on the DGX worker instead, the worker holds the lease, fenced by
`suggest_lease_token_hash` exactly as the media and transcription queues are.

Revision ID: 0021_detail_suggestions
Revises: 0020_voice_transcripts
Create Date: 2026-10-24

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0021_detail_suggestions"
down_revision: Union[str, None] = "0020_voice_transcripts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE voice_transcripts
            ADD COLUMN suggest_status text NOT NULL DEFAULT 'pending'
                CHECK (suggest_status IN ('pending', 'running', 'done', 'failed', 'skipped')),
            ADD COLUMN suggest_attempts int NOT NULL DEFAULT 0,
            ADD COLUMN suggest_run_after timestamptz NOT NULL DEFAULT now(),
            ADD COLUMN suggest_lease_until timestamptz,
            ADD COLUMN suggest_lease_token_hash text,
            ADD COLUMN suggest_error text;
        -- Only a transcript with words in it has anything to suggest from.
        UPDATE voice_transcripts SET suggest_status = 'skipped' WHERE outcome <> 'transcribed';
        CREATE INDEX ix_voice_transcripts_suggest ON voice_transcripts (created_at, id)
            WHERE suggest_status IN ('pending', 'running');

        CREATE TABLE detail_suggestions (
            id uuid PRIMARY KEY,
            capture_id uuid NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
            transcript_id uuid NOT NULL REFERENCES voice_transcripts(id) ON DELETE CASCADE,
            instance_id uuid REFERENCES animal_instances(id) ON DELETE CASCADE,
            field text NOT NULL CHECK (field IN ('sex', 'ear_notch', 'condition', 'known_name')),
            value text NOT NULL,
            quote text NOT NULL,
            model text NOT NULL,
            status text NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'accepted', 'corrected', 'dismissed')),
            decided_value text,
            decided_by uuid REFERENCES observers(id) ON DELETE SET NULL,
            decided_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE NULLS NOT DISTINCT (transcript_id, instance_id, field)
        );
        CREATE INDEX ix_detail_suggestions_capture ON detail_suggestions (capture_id)
            WHERE status = 'open';
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS detail_suggestions;")
    op.execute("DROP INDEX IF EXISTS ix_voice_transcripts_suggest;")
    op.execute(
        """
        ALTER TABLE voice_transcripts
            DROP COLUMN IF EXISTS suggest_error,
            DROP COLUMN IF EXISTS suggest_lease_token_hash,
            DROP COLUMN IF EXISTS suggest_lease_until,
            DROP COLUMN IF EXISTS suggest_run_after,
            DROP COLUMN IF EXISTS suggest_attempts,
            DROP COLUMN IF EXISTS suggest_status;
        """
    )
