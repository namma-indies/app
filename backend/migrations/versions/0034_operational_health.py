"""Private operational health, without request or animal data (#200 / #187)."""
from alembic import op

revision = "0034_operational_health"
down_revision = "0033_blocking"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE worker_heartbeats (
            capability text PRIMARY KEY CHECK (capability IN
                ('reid', 'multi_animal', 'transcription', 'suggestions')),
            seen_at timestamptz NOT NULL
        );
        CREATE TABLE operational_errors (
            minute timestamptz NOT NULL,
            kind text NOT NULL CHECK (kind IN ('http_5xx', 'application_error', 'client_error')),
            count bigint NOT NULL CHECK (count > 0),
            PRIMARY KEY (minute, kind)
        );
        GRANT SELECT, INSERT, UPDATE, DELETE ON worker_heartbeats, operational_errors TO app_rw;
    """)


def downgrade():
    op.execute("DROP TABLE operational_errors; DROP TABLE worker_heartbeats;")
