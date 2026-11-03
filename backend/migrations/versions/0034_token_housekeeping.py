"""Allow the application role to manage token history (#209).

The initial read/write grant in 0001 predates login_tokens (0003). The
restricted role needs the same token DML as sign-in and account deletion,
including SELECT/DELETE for expiry housekeeping. No table layout changes.

Revision ID: 0034_token_housekeeping
Revises: 0033_blocking
Create Date: 2026-11-03

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0034_token_housekeeping"
down_revision: Union[str, None] = "0033_blocking"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON login_tokens TO app_rw;")


def downgrade() -> None:
    op.execute("REVOKE SELECT, INSERT, UPDATE, DELETE ON login_tokens FROM app_rw;")
