"""Moderation/ops review fixes.

Revision ID: 0046_moderation_fixes
Revises: 0045_match_evidence

* sighting_reports.last_resolved_at remembers that a reporter's earlier report
  was ruled on, after the row is reopened, so the same account cannot re-hide
  an approved capture on its own.
"""
from alembic import op

revision = "0046_moderation_fixes"
down_revision = "0045_match_evidence"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE sighting_reports ADD COLUMN last_resolved_at timestamptz;
    """)


def downgrade():
    op.execute("""
        ALTER TABLE sighting_reports DROP COLUMN last_resolved_at;
    """)
