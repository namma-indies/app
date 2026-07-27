"""email sign-in: stable observer identity + single-use login tokens

The passcode gate creates a new observer on every join, so one person becomes
many identities and their sightings split. Email is a real key, so observers
can be looked up by it instead of minted blind. Login tokens are stored as
SHA-256 hashes and burned on use.

Revision ID: 0003_email_auth
Revises: 0002_dog_confidence_label
Create Date: 2026-07-27

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003_email_auth"
down_revision: Union[str, None] = "0002_dog_confidence_label"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Plaintext, not hashed: we need the address to send to it, so a hash
    # sitting beside the plaintext would buy nothing. Revisit at open signup.
    op.execute("ALTER TABLE observers ADD COLUMN email text;")
    # Partial unique: passcode observers have no email and must not collide
    # with each other on NULL.
    op.execute(
        "CREATE UNIQUE INDEX ux_observers_email ON observers (email) "
        "WHERE email IS NOT NULL;"
    )
    # token_hash is the PK: we look up only by hash, never by raw token.
    op.execute(
        """
        CREATE TABLE login_tokens (
            token_hash text PRIMARY KEY,
            observer_id uuid NOT NULL REFERENCES observers(id) ON DELETE CASCADE,
            expires_at timestamptz NOT NULL,
            used_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    # Sweeping expired tokens is the only query that isn't by PK.
    op.execute("CREATE INDEX ix_login_tokens_expires_at ON login_tokens (expires_at);")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS login_tokens;")
    op.execute("DROP INDEX IF EXISTS ux_observers_email;")
    op.execute("ALTER TABLE observers DROP COLUMN IF EXISTS email;")
