"""match_proposals must remember which sighting it matched, not just which
individual

`match_proposals` records `candidate_individual_id`, which is enough once the
population has identities. It is not enough at the start, when every stored
sighting is still `unmatched` and therefore has no individual: the proposal then
says "this looks like something" without recording what, and a `same` verdict
has nothing to attach the new sighting to.

That is exactly the bootstrap case -- the first confirmed match in the system
is necessarily between two identity-less sightings, and it is the moment an
individual should be created.

So proposals now carry the candidate sighting as well. `candidate_individual_id`
stays: when the candidate already belongs to an individual, that is the more
useful target and avoids a second lookup.

Revision ID: 0006_proposal_candidate_sighting
Revises: 0005_miewid_embedding_column
Create Date: 2026-08-08

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0006_proposal_candidate_sighting"
down_revision: Union[str, None] = "0005_miewid_embedding_column"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE match_proposals ADD COLUMN candidate_sighting_id uuid "
        "REFERENCES sightings(id) ON DELETE CASCADE;"
    )
    # A proposal must point at something reviewable: either a known individual
    # or the specific sighting it resembled. Without this a row can exist that
    # a human cannot act on.
    op.execute(
        "ALTER TABLE match_proposals ADD CONSTRAINT ck_match_proposals_has_target "
        "CHECK (candidate_individual_id IS NOT NULL "
        "       OR candidate_sighting_id IS NOT NULL);"
    )
    # The review queue is "pending proposals for this sighting", and the
    # duplicate-suppression path looks up by sighting too.
    op.execute(
        "CREATE INDEX ix_match_proposals_sighting_status "
        "ON match_proposals (sighting_id, status);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_match_proposals_sighting_status;")
    op.execute(
        "ALTER TABLE match_proposals "
        "DROP CONSTRAINT IF EXISTS ck_match_proposals_has_target;"
    )
    op.execute(
        "ALTER TABLE match_proposals DROP COLUMN IF EXISTS candidate_sighting_id;"
    )
