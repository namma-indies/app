"""Durable media jobs with fenced leases; additive, legacy jobs untouched."""
from alembic import op

revision = "0011_media_jobs"
down_revision = "0010_sighting_reports"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE jobs
          ADD COLUMN sighting_id uuid REFERENCES sightings(id) ON DELETE CASCADE,
          ADD COLUMN pipeline_version text,
          ADD COLUMN lease_token_hash text,
          ADD COLUMN lease_owner text,
          ADD COLUMN lease_expires_at timestamptz,
          ADD COLUMN cpu_eligible_at timestamptz,
          ADD COLUMN completion_digest text,
          ADD COLUMN terminal_outcome text CHECK (terminal_outcome IN ('ready','no_animal','failed')),
          ADD CONSTRAINT media_job_kind CHECK (sighting_id IS NULL OR (kind IN ('photo','video') AND pipeline_version IS NOT NULL));
        CREATE UNIQUE INDEX ix_jobs_sighting_pipeline ON jobs(sighting_id,pipeline_version)
          WHERE sighting_id IS NOT NULL;
        CREATE INDEX ix_jobs_media_claim ON jobs(status,run_after,created_at)
          WHERE sighting_id IS NOT NULL AND status IN ('pending','running');
        ALTER TABLE sightings ADD COLUMN processing_state text NOT NULL DEFAULT 'legacy'
          CHECK (processing_state IN ('legacy','queued','processing','ready','no_animal','failed'));
    """)


def downgrade():
    op.execute("""
        ALTER TABLE sightings DROP COLUMN processing_state;
        DROP INDEX ix_jobs_media_claim;
        DROP INDEX ix_jobs_sighting_pipeline;
        ALTER TABLE jobs DROP CONSTRAINT media_job_kind,
          DROP COLUMN terminal_outcome, DROP COLUMN completion_digest,
          DROP COLUMN cpu_eligible_at, DROP COLUMN lease_expires_at,
          DROP COLUMN lease_owner, DROP COLUMN lease_token_hash,
          DROP COLUMN pipeline_version, DROP COLUMN sighting_id;
    """)
