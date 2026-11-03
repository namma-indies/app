"""Shared auth budgets and single-use passcode proof-of-work.

Revision ID: 0034_auth_limits
Revises: 0033_blocking
"""
from alembic import op

revision = "0034_auth_limits"
down_revision = "0033_blocking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE rate_limit_buckets (
            key_hash bytea PRIMARY KEY,
            hits bigint NOT NULL,
            expires_at timestamptz NOT NULL
        );
        CREATE INDEX ix_rate_limit_expiry ON rate_limit_buckets (expires_at);
        CREATE TABLE auth_failures (
            key_hash bytea PRIMARY KEY,
            failures integer NOT NULL,
            blocked_until timestamptz NOT NULL,
            expires_at timestamptz NOT NULL
        );
        CREATE INDEX ix_auth_failure_expiry ON auth_failures (expires_at);
        CREATE TABLE join_pow_uses (
            challenge_hash bytea PRIMARY KEY,
            expires_at timestamptz NOT NULL
        );
        CREATE INDEX ix_join_pow_expiry ON join_pow_uses (expires_at);
        GRANT SELECT, INSERT, UPDATE, DELETE ON
            rate_limit_buckets, auth_failures, join_pow_uses TO app_rw;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE join_pow_uses, auth_failures, rate_limit_buckets")
