"""Public grievance intake and provisional deadline clocks (#199).

Revision ID: 0035_grievances
Revises: 0034_moderation_ops
"""
from alembic import op

revision = "0035_grievances"
down_revision = "0034_moderation_ops"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE grievances (
            id uuid PRIMARY KEY,
            reference text NOT NULL UNIQUE,
            submission_key uuid NOT NULL UNIQUE,
            payload_digest text NOT NULL,
            kind text NOT NULL CHECK (kind IN ('takedown','privacy','intimate_imagery','government_order','other')),
            target text NOT NULL CHECK (length(target) BETWEEN 1 AND 1000),
            -- A complaint may name an unknown or already-erased sighting.
            -- No FK: erasure must not take a grievance lock while holding a
            -- capture lock (moderator actions acquire those in reverse order).
            sighting_id uuid,
            details text NOT NULL CHECK (length(details) BETWEEN 1 AND 5000),
            contact_email text NOT NULL CHECK (length(contact_email) BETWEEN 1 AND 254),
            status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','in_review','resolved','dismissed')),
            received_at timestamptz NOT NULL DEFAULT now(),
            acknowledge_due_at timestamptz NOT NULL,
            resolve_due_at timestamptz NOT NULL,
            removal_due_at timestamptz,
            acknowledgement_mail_id uuid REFERENCES moderation_mail(id) ON DELETE SET NULL,
            removed_at timestamptz,
            resolved_at timestamptz,
            resolved_by uuid,
            resolution_reason text,
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_grievances_open ON grievances (removal_due_at, resolve_due_at)
            WHERE resolved_at IS NULL;
        GRANT SELECT, INSERT, UPDATE, DELETE ON grievances TO app_rw;
    """)


def downgrade():
    op.execute("DROP TABLE grievances")
