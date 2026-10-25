"""Transcribe voice notes and clip sound on the GPU worker.

Two tables, kept apart on purpose:

- `transcription_jobs` is the queue the DGX worker drains. It is not the
  `jobs` table: those rows drive `processing_state` on sightings and captures,
  and every claim query over them assumes an image pipeline. A transcript is
  never on the path to a sighting appearing or being matched, so it gets its
  own queue that cannot hold that path up or be mistaken for part of it.
- `voice_transcripts` is the result: what was said, in what language, by which
  model. One row per (capture, source, pipeline_version), so a better model
  later writes new rows beside the old ones instead of overwriting them.

Both hang off the capture. Every sighting has one (0015's trigger gives a
/sighting upload a stand-in capture with the sighting's own id), and a /capture
upload's note is shared by all the sightings it publishes.

`source` says which sound it was: a voice note recorded with photos, or the
sound track of a clip. `s3_key` is copied onto the job so the worker never
needs to know which table holds the media.

`outcome` separates three answers that all have empty text: speech was found
and transcribed, the audio held no speech, and the clip had no sound track.

Revision ID: 0020_voice_transcripts
Revises: 0019_voice_notes
Create Date: 2026-10-24

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0020_voice_transcripts"
down_revision: Union[str, None] = "0019_voice_notes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE transcription_jobs (
            id uuid PRIMARY KEY,
            capture_id uuid NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
            source text NOT NULL CHECK (source IN ('note', 'clip')),
            s3_key text NOT NULL,
            pipeline_version text NOT NULL,
            status text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'done', 'failed')),
            attempts int NOT NULL DEFAULT 0,
            run_after timestamptz NOT NULL DEFAULT now(),
            lease_token_hash text,
            lease_owner text,
            lease_expires_at timestamptz,
            last_error text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (capture_id, source, pipeline_version)
        );
        CREATE INDEX ix_transcription_jobs_open ON transcription_jobs (created_at, id)
            WHERE status IN ('pending', 'running');

        CREATE TABLE voice_transcripts (
            id uuid PRIMARY KEY,
            capture_id uuid NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
            source text NOT NULL CHECK (source IN ('note', 'clip')),
            pipeline_version text NOT NULL,
            model text NOT NULL,
            outcome text NOT NULL CHECK (outcome IN ('transcribed', 'no_speech', 'no_audio')),
            language text,
            text text NOT NULL DEFAULT '',
            segments jsonb NOT NULL DEFAULT '[]',
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (capture_id, source, pipeline_version)
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS voice_transcripts;")
    op.execute("DROP TABLE IF EXISTS transcription_jobs;")
