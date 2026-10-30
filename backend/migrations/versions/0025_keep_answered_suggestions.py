"""An answered suggestion outlives the note it came from (#158).

Deleting a voice note added later removes its audio, its transcript and the
suggestions from it still open. One already answered stays, as the record of
what the model proposed and what the observer chose (`value`,
`decided_value`), which is how its accuracy is measured. It loses the link to
the deleted transcript and the words it quoted (`quote` is emptied by the
delete itself), since those were the recording's.

So `transcript_id` becomes nullable and is set NULL with its transcript, and
the one-suggestion-per-detail key applies only while it has one: several
answered suggestions from deleted notes can share an animal and a detail.

`media_purges` is audio owed a delete from the object store: written in the
same transaction that deletes the note, removed once the store confirms. One
the store refused (the API's credentials may lack s3:DeleteObject) waits
there for scripts/purge_media.py, run with credentials that may delete.

Revision ID: 0025_keep_answered_suggestions
Revises: 0024_later_voice_notes
Create Date: 2026-10-30

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0025_keep_answered_suggestions"
down_revision: Union[str, None] = "0024_later_voice_notes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE detail_suggestions
            ALTER COLUMN transcript_id DROP NOT NULL,
            DROP CONSTRAINT detail_suggestions_transcript_id_fkey,
            ADD CONSTRAINT detail_suggestions_transcript_id_fkey
                FOREIGN KEY (transcript_id) REFERENCES voice_transcripts(id) ON DELETE SET NULL,
            DROP CONSTRAINT detail_suggestions_transcript_id_instance_id_field_key;
        CREATE UNIQUE INDEX ux_detail_suggestions_per_transcript
            ON detail_suggestions (transcript_id, instance_id, field) NULLS NOT DISTINCT
            WHERE transcript_id IS NOT NULL;

        CREATE TABLE media_purges (
            s3_key text PRIMARY KEY,
            reason text NOT NULL,
            attempts int NOT NULL DEFAULT 0,
            last_error text,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS media_purges;
        DELETE FROM detail_suggestions WHERE transcript_id IS NULL;
        DROP INDEX IF EXISTS ux_detail_suggestions_per_transcript;
        ALTER TABLE detail_suggestions
            ADD CONSTRAINT detail_suggestions_transcript_id_instance_id_field_key
                UNIQUE NULLS NOT DISTINCT (transcript_id, instance_id, field),
            DROP CONSTRAINT detail_suggestions_transcript_id_fkey,
            ADD CONSTRAINT detail_suggestions_transcript_id_fkey
                FOREIGN KEY (transcript_id) REFERENCES voice_transcripts(id) ON DELETE CASCADE,
            ALTER COLUMN transcript_id SET NOT NULL;
        """
    )
