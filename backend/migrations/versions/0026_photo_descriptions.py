"""AI-written descriptions of the animal in a sighting with no note (#160).

For a sighting nobody said or wrote anything about, the local model looks at
the animal's photos and writes a line or two about the animal alone, and
suggests an ear notch or a condition from them. It proposes, never decides:
the observer uses, edits or dismisses the description (`decision`), and
answers each suggestion as for a voice note.

`photo_descriptions` is one row per sighting and is also the work queue for
the model call, leased by time and fenced by token like
`voice_transcripts.suggest_*`, and drained by the DGX worker through the
same /internal/suggestion-jobs route. `backfill` marks a row queued by
scripts/describe_backfill.py for a sighting from before this: those are
handed out after everything else, at a capped rate.

A description that breaks the privacy rules (app/describe.py) is never
stored: `dropped` records which rule, never the words.

`detail_suggestions.description_id` is the description a suggestion was read
from; it has a transcript or a description, never both.

Revision ID: 0026_photo_descriptions
Revises: 0025_keep_answered_suggestions
Create Date: 2026-10-30

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0026_photo_descriptions"
down_revision: Union[str, None] = "0025_keep_answered_suggestions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE photo_descriptions (
            id uuid PRIMARY KEY,
            sighting_id uuid NOT NULL UNIQUE REFERENCES sightings(id) ON DELETE CASCADE,
            capture_id uuid NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
            backfill boolean NOT NULL DEFAULT false,
            status text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'done', 'failed', 'skipped')),
            attempts int NOT NULL DEFAULT 0,
            run_after timestamptz NOT NULL DEFAULT now(),
            first_claimed_at timestamptz,
            lease_until timestamptz,
            lease_token_hash text,
            error text,
            description text,
            model text,
            dropped text,
            decision text NOT NULL DEFAULT 'open'
                CHECK (decision IN ('open', 'accepted', 'edited', 'dismissed')),
            decided_text text,
            decided_by uuid REFERENCES observers(id) ON DELETE SET NULL,
            decided_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_photo_descriptions_queue ON photo_descriptions (backfill, created_at, id)
            WHERE status IN ('pending', 'running');

        ALTER TABLE detail_suggestions
            ADD COLUMN description_id uuid REFERENCES photo_descriptions(id) ON DELETE CASCADE,
            ADD CONSTRAINT detail_suggestions_one_source
                CHECK (transcript_id IS NULL OR description_id IS NULL);
        CREATE UNIQUE INDEX ux_detail_suggestions_per_description
            ON detail_suggestions (description_id, instance_id, field) NULLS NOT DISTINCT
            WHERE description_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM detail_suggestions WHERE description_id IS NOT NULL;
        DROP INDEX IF EXISTS ux_detail_suggestions_per_description;
        ALTER TABLE detail_suggestions
            DROP CONSTRAINT IF EXISTS detail_suggestions_one_source,
            DROP COLUMN IF EXISTS description_id;
        DROP TABLE IF EXISTS photo_descriptions;
        """
    )
