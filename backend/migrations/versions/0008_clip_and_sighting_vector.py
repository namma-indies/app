"""Keep the clip, and give a sighting one vector of its own.

TWO CHANGES, ONE REASON: a clip is evidence about a *sighting*, not about any
one of the frames pulled out of it.

`sightings.clip_s3_key` -- the clip is now retained instead of discarded after
frame extraction. Only the key lives here; the bytes go to S3 beside the
photos, as images already do. Retaining it buys the one thing the old design
could never do: re-run a better detector or a newer embedding model over the
original footage. Once frames were chosen and the clip dropped, every frame not
chosen was gone for good.

`sightings.vec_miew` -- the mean of the per-frame vectors, L2-normalised.

Averaging is right *here* and wrong across sightings, which is worth spelling
out because the two look identical in code. Frames a second apart from one clip
are one view of an animal sampled repeatedly, so their mean is that view with
the noise averaged down. Two sightings days apart are different views, and
averaging those blurs both into neither -- which is why `routes/dogs.py`
compares dogs by the max over photo pairs and has a test that fails if someone
switches it to a centroid. Same arithmetic, opposite conclusion, because the
scope differs.

The per-photo rows in `embeddings` stay. They are what the max-over-frames
search already uses, they are the only per-frame record once a mean exists, and
a sighting-level vector cannot be recomputed from a mean if the frames are
gone.

Revision ID: 0008_clip_and_sighting_vector
Revises: 0007_geo_source_exif
Create Date: 2026-08-29

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0008_clip_and_sighting_vector"
down_revision: Union[str, None] = "0007_geo_source_exif"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MIEWID_DIM = 2152


def upgrade() -> None:
    op.execute("ALTER TABLE sightings ADD COLUMN clip_s3_key text;")
    op.execute(f"ALTER TABLE sightings ADD COLUMN vec_miew vector({MIEWID_DIM});")

    # Built on a halfvec cast: HNSW refuses >2000 dimensions and MiewID is
    # 2152. Queries must use the identical cast expression to hit this index --
    # otherwise Postgres falls back to a sequential scan over every sighting,
    # which is correct and quietly terrible.
    op.execute(
        "CREATE INDEX ix_sightings_vec_miew_hnsw ON sightings "
        f"USING hnsw ((vec_miew::halfvec({MIEWID_DIM})) halfvec_cosine_ops);"
    )

    # Partial: the interesting query is "sightings that have a vector", and a
    # photo-only sighting never will.
    op.execute(
        "CREATE INDEX ix_sightings_has_vec ON sightings (id) "
        "WHERE vec_miew IS NOT NULL;"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_sightings_has_vec;")
    op.execute("DROP INDEX IF EXISTS ix_sightings_vec_miew_hnsw;")
    op.execute("ALTER TABLE sightings DROP COLUMN IF EXISTS vec_miew;")
    op.execute("ALTER TABLE sightings DROP COLUMN IF EXISTS clip_s3_key;")
