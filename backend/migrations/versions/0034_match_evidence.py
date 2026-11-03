"""Keep the photo that scored, and the query views behind a proposal (#220).

Old proposals have no evidence snapshot and retain the earliest-photo fallback.
The query IDs are resolved only within the query sighting when read. Deleted
gallery evidence becomes NULL without removing a human verdict's audit trail.
"""

from alembic import op

revision = "0034_match_evidence"
down_revision = "0033_blocking"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE match_proposals
            ADD COLUMN matched_photo_id uuid REFERENCES photos(id) ON DELETE SET NULL,
            ADD COLUMN matched_instance_id uuid REFERENCES animal_instances(id) ON DELETE SET NULL,
            ADD COLUMN query_photo_ids uuid[];
        CREATE INDEX ix_proposals_matched_photo ON match_proposals(matched_photo_id);
        CREATE INDEX ix_proposals_matched_instance ON match_proposals(matched_instance_id);
    """)


def downgrade():
    op.execute("""
        ALTER TABLE match_proposals DROP COLUMN query_photo_ids,
            DROP COLUMN matched_instance_id, DROP COLUMN matched_photo_id;
    """)
