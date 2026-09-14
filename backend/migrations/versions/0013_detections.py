"""One score per (photo, model), and a human verdict that outranks it.

`sightings.dog_confidence` (0002) recorded a number and not which model
produced it. Older rows were scored by YOLOv8n and newer ones by YOLO26x, and
the two disagree badly -- v8n scored a clearly visible dog at 0.021 where 26x
gives 0.800. So the column cannot be filtered on: a naive threshold would
silently hide real dogs whose only crime was being uploaded early.

`detections` fixes that by construction. `model` is part of the key, so a
future detector adds rows rather than invalidating them, the old numbers stay
readable as history, and the backfill gets a resume key that is not `IS NULL`
-- the trap `backfill_embeddings.py` documents in its own docstring.

`animal_confidence` is the denormalised max over a sighting's photos that the
read surfaces filter on; `/map` and `/dogs` are geo and count queries and
should not join per row.

`animal_override` is a moderator saying the detector is wrong, either way. It
is deliberately NOT `review_status`: that column, and `reviewed_at`, mean "a
person ruled on a report" (0010, #66), and a model writing them would erase
that. Keeping the verdict separate also means it survives a threshold retune
and the next detector swap -- which is what makes reviewing the corpus a
thing you do once.

`dog_confidence` is superseded but NOT dropped here. It is the only record of
the old scores, and it stays until the rescore has run in production.

Revision ID: 0013_detections
Revises: 0012_individual_names
Create Date: 2026-09-13

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0013_detections"
down_revision: Union[str, None] = "0012_individual_names"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE detections (
            photo_id   uuid NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
            model      text NOT NULL,
            dog        real NOT NULL,
            cat        real NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (photo_id, model)
        );
        """
    )
    # The backfill's pending query is "photos with no row for THIS model", and
    # the recompute reads every row of one model for one sighting's photos.
    op.execute("CREATE INDEX ix_detections_model ON detections (model);")

    op.execute("ALTER TABLE sightings ADD COLUMN animal_confidence real;")
    op.execute("ALTER TABLE sightings ADD COLUMN animal_override boolean;")
    op.execute("ALTER TABLE sightings ADD COLUMN animal_reviewed_at timestamptz;")
    op.execute(
        "ALTER TABLE sightings ADD COLUMN animal_reviewed_by uuid "
        "REFERENCES observers(id);"
    )
    # The moderator queue is "scored, and nobody has ruled", lowest first.
    # Rows already ruled on are not candidates for it, so the index is partial.
    op.execute(
        "CREATE INDEX ix_sightings_animal_confidence ON sightings (animal_confidence) "
        "WHERE animal_confidence IS NOT NULL AND animal_override IS NULL;"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_sightings_animal_confidence;")
    for col in ("animal_reviewed_by", "animal_reviewed_at",
                "animal_override", "animal_confidence"):
        op.execute(f"ALTER TABLE sightings DROP COLUMN IF EXISTS {col};")
    op.execute("DROP TABLE IF EXISTS detections;")
