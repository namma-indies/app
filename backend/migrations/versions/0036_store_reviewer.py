"""One permanently marked store-reviewer account.

Revision ID: 0036_store_reviewer
Revises: 0035_signup_controls
"""
from alembic import op

revision = "0036_store_reviewer"
down_revision = "0035_signup_controls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE observers ADD COLUMN store_reviewer boolean NOT NULL DEFAULT false;
        CREATE UNIQUE INDEX ix_one_store_reviewer ON observers (store_reviewer) WHERE store_reviewer;
    """)


def downgrade() -> None:
    op.execute("DROP INDEX ix_one_store_reviewer; ALTER TABLE observers DROP COLUMN store_reviewer")
