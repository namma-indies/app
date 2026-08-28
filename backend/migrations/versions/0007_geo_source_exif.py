"""allow geo_source = 'exif' for camera-roll imports

A photo imported from the camera roll was taken somewhere else, some time ago.
Its coordinates come from the file's EXIF rather than a live GPS fix, and that
distinction has to survive into the row: EXIF coordinates are typically less
accurate than a device fix, can be minutes-to-hours stale relative to the
capture, and -- unlike `device_gps` -- were not observed by us at all.

`resolve_sighting` filters candidates by a 1km PostGIS radius, so where a
sighting claims to be is load-bearing for re-identification, not decoration.
Recording an import as `device_gps` would assert a confidence we do not have;
recording it as `pin` would claim a human placed it on a map. Hence a fourth
value rather than reusing one of the three.

`geo_accuracy_m` stays NULL for these, since EXIF carries no accuracy estimate
worth trusting.

Revision ID: 0007_geo_source_exif
Revises: 0006_proposal_candidate_sighting
Create Date: 2026-08-28

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0007_geo_source_exif"
down_revision: Union[str, None] = "0006_proposal_candidate_sighting"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Postgres has no "add a value to a CHECK constraint", so the constraint is
    # replaced. Named explicitly rather than relying on the implicit name
    # Postgres generated in 0001, which is stable in practice but not promised.
    op.execute("ALTER TABLE sightings DROP CONSTRAINT IF EXISTS sightings_geo_source_check;")
    op.execute(
        "ALTER TABLE sightings ADD CONSTRAINT sightings_geo_source_check "
        "CHECK (geo_source IN ('device_gps','pin','none','exif'));"
    )


def downgrade() -> None:
    # Rows that only this migration made legal have to go somewhere before the
    # narrower constraint can be re-applied, or the ALTER fails on live data.
    # 'pin' is the honest destination: a coordinate we did not observe
    # ourselves. Losing the distinction is the cost of going backwards.
    op.execute("UPDATE sightings SET geo_source = 'pin' WHERE geo_source = 'exif';")
    op.execute("ALTER TABLE sightings DROP CONSTRAINT IF EXISTS sightings_geo_source_check;")
    op.execute(
        "ALTER TABLE sightings ADD CONSTRAINT sightings_geo_source_check "
        "CHECK (geo_source IN ('device_gps','pin','none'));"
    )
