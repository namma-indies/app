"""Voice notes added to a sighting or capture after it was uploaded (#156).

A note recorded at upload stays where 0019 put it (`audio_s3_key` on the
sighting or capture): nothing about those changes. A note added later is a
`voice_notes` row of its own, because there can be several, and each is
transcribed and read for suggestions exactly as an upload's note is.

`client_token` is the recording's own id, made by the app when it was
recorded: a save retried after its answer was lost finds the note it already
made instead of making another (unique per observer, as captures' is).

`sighting_id` is the animal the note was recorded about: set when it was
added from that sighting in the Journal, NULL when added to the whole capture
from its details sheet. On a multi-animal capture a sighting's note is read
for that animal alone (app/suggestions.plan).

`transcription_jobs` and `voice_transcripts` were one note per capture
(UNIQUE on capture, source, pipeline). They gain `note_id`, NULL for the
upload's own note and clip, and the key takes it in with NULLs equal, so the
upload's note is still one row and each later note is one more.

Revision ID: 0024_later_voice_notes
Revises: 0023_transcript_english
Create Date: 2026-10-30

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0024_later_voice_notes"
down_revision: Union[str, None] = "0023_transcript_english"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE voice_notes (
            id uuid PRIMARY KEY,
            capture_id uuid NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
            sighting_id uuid REFERENCES sightings(id) ON DELETE CASCADE,
            observer_id uuid NOT NULL REFERENCES observers(id) ON DELETE CASCADE,
            s3_key text NOT NULL,
            content_type text NOT NULL,
            bytes int NOT NULL CHECK (bytes > 0),
            client_token text,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_voice_notes_capture ON voice_notes (capture_id, created_at);
        CREATE UNIQUE INDEX ux_voice_notes_client_token ON voice_notes (observer_id, client_token)
            WHERE client_token IS NOT NULL;

        ALTER TABLE transcription_jobs
            ADD COLUMN note_id uuid REFERENCES voice_notes(id) ON DELETE CASCADE,
            DROP CONSTRAINT transcription_jobs_capture_id_source_pipeline_version_key,
            ADD CONSTRAINT transcription_jobs_one_per_sound
                UNIQUE NULLS NOT DISTINCT (capture_id, source, note_id, pipeline_version);
        ALTER TABLE voice_transcripts
            ADD COLUMN note_id uuid REFERENCES voice_notes(id) ON DELETE CASCADE,
            DROP CONSTRAINT voice_transcripts_capture_id_source_pipeline_version_key,
            ADD CONSTRAINT voice_transcripts_one_per_sound
                UNIQUE NULLS NOT DISTINCT (capture_id, source, note_id, pipeline_version);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM voice_transcripts WHERE note_id IS NOT NULL;
        DELETE FROM transcription_jobs WHERE note_id IS NOT NULL;
        ALTER TABLE voice_transcripts
            DROP CONSTRAINT voice_transcripts_one_per_sound,
            DROP COLUMN note_id,
            ADD CONSTRAINT voice_transcripts_capture_id_source_pipeline_version_key
                UNIQUE (capture_id, source, pipeline_version);
        ALTER TABLE transcription_jobs
            DROP CONSTRAINT transcription_jobs_one_per_sound,
            DROP COLUMN note_id,
            ADD CONSTRAINT transcription_jobs_capture_id_source_pipeline_version_key
                UNIQUE (capture_id, source, pipeline_version);
        DROP TABLE IF EXISTS voice_notes;
        """
    )
