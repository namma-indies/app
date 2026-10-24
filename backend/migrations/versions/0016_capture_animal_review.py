"""Keep capture-only animal verdicts separate from processing and publication."""
from alembic import op

revision = "0016_capture_animal_review"
down_revision = "0015_multi_animal_captures"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE captures
            ADD COLUMN animal_override boolean,
            ADD COLUMN animal_reviewed_at timestamptz,
            ADD COLUMN animal_reviewed_by uuid REFERENCES observers(id);
        CREATE INDEX ix_captures_animal_audit ON captures(created_at DESC, id DESC)
            WHERE processing_state <> 'legacy';
        CREATE INDEX ix_sightings_animal_audit ON sightings(created_at DESC, id DESC);
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_rw') THEN
                GRANT SELECT (animal_override, animal_reviewed_at, animal_reviewed_by),
                      UPDATE (animal_override, animal_reviewed_at, animal_reviewed_by)
                    ON captures TO app_rw;
            END IF;
        END $$;
    """)


def downgrade():
    op.execute("""
        DROP INDEX ix_sightings_animal_audit;
        DROP INDEX ix_captures_animal_audit;
        ALTER TABLE captures DROP COLUMN animal_reviewed_by,
            DROP COLUMN animal_reviewed_at, DROP COLUMN animal_override;
    """)
