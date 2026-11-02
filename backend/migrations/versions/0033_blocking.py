"""Blocking another observer, and a moderator's ban (#180).

`observer_blocks`: one person's block of another. It hides each from the
other -- their sightings, photos, names and match pairs -- in both directions
and silently (app/aggregates.py `visible_to`). It changes nothing for anyone
else, so it cannot be used to take something off the map. `id` is what the
blocker's list and unblock use, so the other person's observer id never has
to reach a client. `via_sighting_id` is what was on screen when they blocked,
for a moderator reading the count; SET NULL, as that sighting may go.

`observers.banned_at`, `banned_by`, `ban_reason`: a moderator's ban. It hides
everything the person logged from everyone, ends their sessions, and refuses
their address at sign-in. Reversible.

`banned_emails`: a keyed hash of a banned person's address, so the ban
outlives their account being deleted (their right) without keeping the
address itself.

Revision ID: 0033_blocking
Revises: 0032_account_deletion
Create Date: 2026-11-02

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0033_blocking"
down_revision: Union[str, None] = "0032_account_deletion"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE observer_blocks (
            id uuid PRIMARY KEY,
            blocker_id uuid NOT NULL REFERENCES observers(id) ON DELETE CASCADE,
            blocked_id uuid NOT NULL REFERENCES observers(id) ON DELETE CASCADE,
            via_sighting_id uuid REFERENCES sightings(id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (blocker_id, blocked_id),
            CHECK (blocker_id <> blocked_id)
        );
        CREATE INDEX ix_observer_blocks_blocked ON observer_blocks (blocked_id);
        GRANT SELECT, INSERT, UPDATE, DELETE ON observer_blocks TO app_rw;

        ALTER TABLE observers
            ADD COLUMN banned_at timestamptz,
            ADD COLUMN banned_by uuid REFERENCES observers(id),
            ADD COLUMN ban_reason text;

        CREATE TABLE banned_emails (
            email_hmac text PRIMARY KEY,
            banned_at timestamptz NOT NULL DEFAULT now()
        );
        GRANT SELECT, INSERT, UPDATE, DELETE ON banned_emails TO app_rw;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS banned_emails;
        ALTER TABLE observers DROP COLUMN IF EXISTS ban_reason,
            DROP COLUMN IF EXISTS banned_by, DROP COLUMN IF EXISTS banned_at;
        DROP TABLE IF EXISTS observer_blocks;
        """
    )
