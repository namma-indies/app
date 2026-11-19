"""Grievance category for appealing a suspension.

Revision ID: 0048_moderation_defaults
Revises: 0047_moderation_fixes

* grievances.kind gains 'appeal' ("Appeal a suspension"), on the ordinary
  clock: acknowledge and resolve windows as for 'other', no removal clock.
* An appeal names an account, not content, so its link/sighting field may be
  empty; every other kind still needs one.

Downgrading refuses while any appeal is stored: there is no earlier category
that means the same thing, and rewriting a complaint's type would misstate it.
"""
from alembic import op

revision = "0048_moderation_defaults"
down_revision = "0047_moderation_fixes"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE grievances DROP CONSTRAINT grievances_kind_check;
        ALTER TABLE grievances ADD CONSTRAINT grievances_kind_check
            CHECK (kind IN ('takedown','privacy','intimate_imagery','government_order','other','appeal'));
        ALTER TABLE grievances DROP CONSTRAINT grievances_target_check;
        ALTER TABLE grievances ADD CONSTRAINT grievances_target_check
            CHECK (length(target) <= 1000 AND (length(target) >= 1 OR kind = 'appeal'));
    """)


def downgrade():
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM grievances WHERE kind = 'appeal') THEN
                RAISE EXCEPTION 'grievances holds suspension appeals; export and remove them before downgrading';
            END IF;
        END $$;
        ALTER TABLE grievances DROP CONSTRAINT grievances_target_check;
        ALTER TABLE grievances ADD CONSTRAINT grievances_target_check CHECK (length(target) BETWEEN 1 AND 1000);
        ALTER TABLE grievances DROP CONSTRAINT grievances_kind_check;
        ALTER TABLE grievances ADD CONSTRAINT grievances_kind_check
            CHECK (kind IN ('takedown','privacy','intimate_imagery','government_order','other'));
    """)
