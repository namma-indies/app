"""Deleting a sighting or a whole upload (#54, part of #179).

Two foreign keys had no ON DELETE rule and made deletion impossible:

- `confirmations.proposal_id`. A proposal goes with either of its sightings
  (CASCADE on both sides), but a verdict recorded against the *other* sighting
  pointed at it with no rule, so deleting any sighting that had been the
  candidate in a decided proposal was refused. The verdict is about the dog and
  outlives the pair it was given on: SET NULL.
- `clinical_records.sighting_id`. Unused, but it would refuse the same way.
  A clinical record is about the animal: SET NULL.

`media_purges.kind`: a whole upload is deleted from the store by its prefix
(`captures/{id}/`), so thumbnails, crops and anything a row forgot to name go
with it. A `key` row is one object, as before.

`x_post_purges`: posts on X made from a photo that has since been deleted, owed
a delete there. Written in the same transaction as the erasure, cleared once X
confirms, retried by the daily post run.

Revision ID: 0031_erasure
Revises: 0030_transcript_backfill
Create Date: 2026-11-02

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0031_erasure"
down_revision: Union[str, None] = "0030_transcript_backfill"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE confirmations
            DROP CONSTRAINT confirmations_proposal_id_fkey,
            ADD CONSTRAINT confirmations_proposal_id_fkey
                FOREIGN KEY (proposal_id) REFERENCES match_proposals(id) ON DELETE SET NULL;
        ALTER TABLE clinical_records
            DROP CONSTRAINT clinical_records_sighting_id_fkey,
            ADD CONSTRAINT clinical_records_sighting_id_fkey
                FOREIGN KEY (sighting_id) REFERENCES sightings(id) ON DELETE SET NULL;

        ALTER TABLE media_purges
            ADD COLUMN kind text NOT NULL DEFAULT 'key' CHECK (kind IN ('key', 'prefix'));

        CREATE TABLE x_post_purges (
            x_post_id text PRIMARY KEY,
            reason text NOT NULL,
            attempts int NOT NULL DEFAULT 0,
            last_error text,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        GRANT SELECT, INSERT, UPDATE, DELETE ON x_post_purges TO app_rw;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS x_post_purges;
        ALTER TABLE media_purges DROP COLUMN IF EXISTS kind;
        ALTER TABLE clinical_records
            DROP CONSTRAINT clinical_records_sighting_id_fkey,
            ADD CONSTRAINT clinical_records_sighting_id_fkey
                FOREIGN KEY (sighting_id) REFERENCES sightings(id);
        ALTER TABLE confirmations
            DROP CONSTRAINT confirmations_proposal_id_fkey,
            ADD CONSTRAINT confirmations_proposal_id_fkey
                FOREIGN KEY (proposal_id) REFERENCES match_proposals(id);
        """
    )
