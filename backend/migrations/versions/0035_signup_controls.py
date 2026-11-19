"""Shared email capacity and aggregate abuse signals.

Revision ID: 0035_signup_controls
Revises: 0034_auth_limits
"""
from alembic import op

revision = "0035_signup_controls"
down_revision = "0034_auth_limits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE email_delivery_state (
            scope text PRIMARY KEY,
            next_at timestamptz NOT NULL DEFAULT now(),
            failures integer NOT NULL DEFAULT 0
        );
        CREATE TABLE email_send_attempts (
            id uuid PRIMARY KEY,
            scope text NOT NULL,
            attempted_at timestamptz NOT NULL DEFAULT statement_timestamp(),
            accepted boolean NOT NULL DEFAULT false
        );
        CREATE INDEX ix_email_attempts_scope_time ON email_send_attempts (scope, attempted_at);
        CREATE INDEX ix_email_attempts_time ON email_send_attempts (attempted_at);
        CREATE TABLE abuse_hourly (
            hour timestamptz NOT NULL,
            event text NOT NULL,
            count bigint NOT NULL DEFAULT 1,
            PRIMARY KEY (hour, event)
        );
        CREATE TABLE abuse_monitor_state (
            singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
            started_at timestamptz NOT NULL DEFAULT now()
        );
        INSERT INTO abuse_monitor_state DEFAULT VALUES;
        GRANT SELECT, INSERT, UPDATE, DELETE ON
            email_delivery_state, email_send_attempts, abuse_hourly TO app_rw;
        GRANT SELECT ON abuse_monitor_state TO app_rw;

        CREATE FUNCTION count_abuse_creation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO abuse_hourly (hour, event)
            VALUES (date_trunc('hour', statement_timestamp(), 'UTC'),
                CASE WHEN TG_TABLE_NAME = 'observers' THEN 'signups' ELSE 'uploads' END)
            ON CONFLICT (hour, event) DO UPDATE SET count = abuse_hourly.count + 1;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER count_signup AFTER INSERT ON observers
            FOR EACH ROW EXECUTE FUNCTION count_abuse_creation();
        CREATE TRIGGER count_upload AFTER INSERT ON captures
            FOR EACH ROW EXECUTE FUNCTION count_abuse_creation();
    """)


def downgrade() -> None:
    op.execute("""
        DROP TRIGGER count_signup ON observers;
        DROP TRIGGER count_upload ON captures;
        DROP FUNCTION count_abuse_creation();
        DROP TABLE abuse_monitor_state, abuse_hourly, email_send_attempts, email_delivery_state;
    """)
