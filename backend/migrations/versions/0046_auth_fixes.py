"""Email budgets by purpose, a shared send-rate token bucket, and sharded
abuse counters.

Revision ID: 0046_auth_fixes
Revises: 0045_match_evidence
"""
from alembic import op

revision = "0046_auth_fixes"
down_revision = "0045_match_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE email_send_attempts
            ADD COLUMN purpose text NOT NULL DEFAULT 'signin'
            CHECK (purpose IN ('signin', 'account'));
        -- tokens starts high; the application caps it at the configured burst.
        ALTER TABLE email_delivery_state
            ADD COLUMN tokens double precision NOT NULL DEFAULT 1000,
            ADD COLUMN refilled_at timestamptz NOT NULL DEFAULT now();

        -- Creation counters were one row per (hour, event): every capture
        -- and every sign-up in an hour upserted the same row, and held its
        -- lock until the inserting transaction committed, serialising all
        -- uploads behind each other. Spread each hour over 16 sub-rows,
        -- picked by backend, so concurrent transactions (always on different
        -- backends) rarely share one. Readers already sum by hour.
        ALTER TABLE abuse_hourly ADD COLUMN shard smallint NOT NULL DEFAULT 0;
        ALTER TABLE abuse_hourly DROP CONSTRAINT abuse_hourly_pkey;
        ALTER TABLE abuse_hourly ADD PRIMARY KEY (hour, event, shard);
        CREATE OR REPLACE FUNCTION count_abuse_creation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO abuse_hourly (hour, event, shard)
            VALUES (date_trunc('hour', statement_timestamp(), 'UTC'),
                CASE WHEN TG_TABLE_NAME = 'observers' THEN 'signups' ELSE 'uploads' END,
                pg_backend_pid() % 16)
            ON CONFLICT (hour, event, shard) DO UPDATE SET count = abuse_hourly.count + 1;
            RETURN NEW;
        END;
        $$;
    """)


def downgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION count_abuse_creation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO abuse_hourly (hour, event)
            VALUES (date_trunc('hour', statement_timestamp(), 'UTC'),
                CASE WHEN TG_TABLE_NAME = 'observers' THEN 'signups' ELSE 'uploads' END)
            ON CONFLICT (hour, event) DO UPDATE SET count = abuse_hourly.count + 1;
            RETURN NEW;
        END;
        $$;
        CREATE TEMP TABLE abuse_hourly_merged AS
            SELECT hour, event, sum(count)::bigint AS count FROM abuse_hourly GROUP BY hour, event;
        DELETE FROM abuse_hourly;
        ALTER TABLE abuse_hourly DROP CONSTRAINT abuse_hourly_pkey;
        ALTER TABLE abuse_hourly DROP COLUMN shard;
        ALTER TABLE abuse_hourly ADD PRIMARY KEY (hour, event);
        INSERT INTO abuse_hourly (hour, event, count) SELECT hour, event, count FROM abuse_hourly_merged;
        DROP TABLE abuse_hourly_merged;
        ALTER TABLE email_delivery_state DROP COLUMN refilled_at, DROP COLUMN tokens;
        ALTER TABLE email_send_attempts DROP COLUMN purpose;
    """)
