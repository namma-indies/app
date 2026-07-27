"""dog confidence as a label, not a gate

The detector used to reject captures below a threshold, and a rejected capture
was never persisted -- no S3 object, no row. Since the PWA captures through a
file input (iOS never writes those frames to the camera roll), the discarded
server copy was the only copy in existence. We now always save and record what
the detector thought, so a wrong call costs a label instead of a sighting.

Revision ID: 0002_dog_confidence_label
Revises: 0001_full_v2_schema
Create Date: 2026-07-26

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002_dog_confidence_label"
down_revision: Union[str, None] = "0001_full_v2_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable on purpose: offline captures sync without ever being scored,
    # and detector failures must not block a save. NULL means "not scored",
    # which is distinct from 0.0 ("scored, saw no dog").
    op.execute("ALTER TABLE sightings ADD COLUMN dog_confidence real;")
    # Partial index: the tuning/review query is "show me the low scorers",
    # and rows we never scored aren't candidates for it.
    op.execute(
        "CREATE INDEX ix_sightings_dog_confidence ON sightings (dog_confidence) "
        "WHERE dog_confidence IS NOT NULL;"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_sightings_dog_confidence;")
    op.execute("ALTER TABLE sightings DROP COLUMN IF EXISTS dog_confidence;")
