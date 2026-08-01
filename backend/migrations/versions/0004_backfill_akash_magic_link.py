"""Claim the pre-passcode magic_link observers for akash@dognosis.tech.

The email door folds an anonymous identity into a verified one only when the
observer was created via the passcode *and* typed their address as their name
(see absorb_passcode_observers). Both tests fail for the two observers minted
by the original magic-link build: they predate the passcode door, and their
display_name is "Akash", not an address. Their 13 sightings were therefore
stranded -- signing in by email produced a correct but empty account.

Done as a pinned one-off rather than by widening the absorb rule. The general
version would have to match unverified display names on a shared-passcode
door, which is a real identity-spoofing surface, and this is a one-time
migration artifact with exactly two known rows. Narrow beats clever here.

The IDs are hardcoded deliberately: this is a statement about specific rows in
one database, not a rule. Anywhere those rows don't exist -- tests, a fresh
deploy -- every statement is a harmless no-op.

Revision ID: 0004_backfill_akash_magic_link
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0004_backfill_akash_magic_link"
down_revision: Union[str, None] = "0003_email_auth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The two magic_link observers, and the verified email observer that owns the
# same person's address.
_SOURCES = (
    "019f94d5-c761-7153-a321-1a5392b81ffd",
    "019f94d6-74f8-7a11-aabd-52e8b5e92709",
)
_TARGET = "019faeb5-9cca-75c0-b800-a6f55a9acead"

# Every column in the schema that points at an observer; mirrors
# app.auth.email_login._OBSERVER_REFS. Miss one and retiring the source rows
# orphans the data hanging off it.
_OBSERVER_REFS = (
    ("sightings", "observer_id"),
    ("individuals", "named_by"),
    ("individuals", "created_by_observer"),
    ("match_proposals", "resolved_by"),
    ("confirmations", "observer_id"),
    ("observers", "created_by_observer"),
)

_SOURCE_LIST = ", ".join(f"'{s}'::uuid" for s in _SOURCES)


def upgrade() -> None:
    # Guarded on the target existing: without it the UPDATEs would point
    # sightings at an absent observer and the FK would fail the migration.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM observers WHERE id = '{_TARGET}'::uuid) THEN
                {"".join(
                    f"UPDATE {t} SET {c} = '{_TARGET}'::uuid "
                    f"WHERE {c} IN ({_SOURCE_LIST}); "
                    for t, c in _OBSERVER_REFS
                )}
                UPDATE observers SET deleted_at = now(), updated_at = now()
                WHERE id IN ({_SOURCE_LIST}) AND deleted_at IS NULL;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    # Not reversible: once the sightings carry the target's id, nothing records
    # which of the two source observers each came from. Un-retiring the source
    # rows is the most that can be honestly undone.
    op.execute(
        f"UPDATE observers SET deleted_at = NULL, updated_at = now() "
        f"WHERE id IN ({_SOURCE_LIST});"
    )
