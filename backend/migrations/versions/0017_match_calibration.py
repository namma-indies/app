"""Frozen, private calibration samples and independent moderator labels."""
from alembic import op

revision = "0017_match_calibration"
down_revision = "0016_capture_animal_review"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE match_calibration_batches (
            id uuid PRIMARY KEY,
            created_at timestamptz NOT NULL DEFAULT now(),
            created_by uuid REFERENCES observers(id) ON DELETE SET NULL,
            eligible_pairs integer NOT NULL CHECK (eligible_pairs >= 0),
            sampled_pairs integer NOT NULL CHECK (sampled_pairs >= 0),
            policy text NOT NULL,
            model text NOT NULL,
            radius_m double precision NOT NULL,
            settings jsonb NOT NULL,
            sampling jsonb NOT NULL
        );
        CREATE TABLE match_calibration_pairs (
            id uuid PRIMARY KEY,
            batch_id uuid NOT NULL REFERENCES match_calibration_batches(id) ON DELETE CASCADE,
            ordinal integer NOT NULL,
            query_id uuid NOT NULL,
            candidate_id uuid NOT NULL,
            score double precision NOT NULL,
            manifest jsonb NOT NULL,
            evidence_hash text NOT NULL,
            stale boolean NOT NULL DEFAULT false,
            UNIQUE(batch_id, ordinal),
            UNIQUE(batch_id, query_id, candidate_id),
            CHECK(query_id <> candidate_id)
        );
        CREATE TABLE match_calibration_labels (
            pair_id uuid NOT NULL REFERENCES match_calibration_pairs(id) ON DELETE CASCADE,
            moderator_id uuid NOT NULL REFERENCES observers(id) ON DELETE CASCADE,
            verdict text NOT NULL CHECK (verdict IN ('same','different','unsure')),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(pair_id, moderator_id)
        );
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app_rw') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON match_calibration_batches,
                    match_calibration_pairs, match_calibration_labels TO app_rw;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='public_read') THEN
                REVOKE ALL ON match_calibration_batches, match_calibration_pairs,
                    match_calibration_labels FROM public_read;
            END IF;
        END $$;
        REVOKE ALL ON match_calibration_batches, match_calibration_pairs,
            match_calibration_labels FROM PUBLIC;
    """)


def downgrade():
    op.execute("DROP TABLE match_calibration_labels, match_calibration_pairs, match_calibration_batches")
