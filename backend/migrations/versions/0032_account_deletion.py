"""Deleting an account (#179).

`observers.deletion_requested_at`: set the moment someone asks. From then on
everything they logged is off every shared surface and their session reaches
only /account, while the grace period runs (Q1).

`account_deletions`: the request, and the state of its purge -- scheduled,
purging, done, or cancelled. One row per observer: asking again after
cancelling reuses it. No foreign key on purpose: it is the record that an
account was erased, and must outlive the observer row should that row ever be
removed outright rather than kept as an anonymous tombstone (app/accounts.py).
It holds no personal data.

`login_tokens.purpose`: a mailed link either signs someone in or confirms
deleting their account, never both. A sign-in link cannot delete an account,
and a deletion link cannot sign anyone in.

Revision ID: 0032_account_deletion
Revises: 0031_erasure
Create Date: 2026-11-02

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0032_account_deletion"
down_revision: Union[str, None] = "0031_erasure"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE observers ADD COLUMN deletion_requested_at timestamptz;

        CREATE TABLE account_deletions (
            observer_id uuid PRIMARY KEY,
            via text NOT NULL CHECK (via IN ('app', 'web')),
            requested_at timestamptz NOT NULL DEFAULT now(),
            purge_after timestamptz NOT NULL,
            status text NOT NULL DEFAULT 'scheduled'
                CHECK (status IN ('scheduled', 'purging', 'done', 'cancelled')),
            attempts int NOT NULL DEFAULT 0,
            last_error text,
            lease_until timestamptz,
            started_at timestamptz,
            completed_at timestamptz,
            cancelled_at timestamptz
        );
        CREATE INDEX ix_account_deletions_due ON account_deletions (purge_after)
            WHERE status IN ('scheduled', 'purging');
        GRANT SELECT, INSERT, UPDATE, DELETE ON account_deletions TO app_rw;

        ALTER TABLE login_tokens
            ADD COLUMN purpose text NOT NULL DEFAULT 'login' CHECK (purpose IN ('login', 'delete'));
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE login_tokens DROP COLUMN IF EXISTS purpose;
        DROP TABLE IF EXISTS account_deletions;
        ALTER TABLE observers DROP COLUMN IF EXISTS deletion_requested_at;
        """
    )
