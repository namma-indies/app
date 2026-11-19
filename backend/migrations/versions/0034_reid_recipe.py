"""Tag vectors and cached means with their re-ID recipe (#221).

Existing per-photo rows for the current model are the baseline recipe. Existing
means deliberately remain untagged: rebuild them from those frames with the
backfill, never assert their provenance by attaching a tag to an old cache.
"""

from alembic import op

revision = "0034_reid_recipe"
down_revision = "0033_blocking"
branch_labels = None
depends_on = None

BASELINE_RECIPE = "stored-webp90-yolo640-miew440-imagenet-l2-mean-v1"


def upgrade():
    op.execute(f"""
        ALTER TABLE embeddings ADD COLUMN recipe text;
        UPDATE embeddings SET recipe='{BASELINE_RECIPE}'
            WHERE model='miewid-msv3' AND vec_miew IS NOT NULL;
        -- A fixed baseline default supports old inserts. Production writers
        -- supply the active recipe explicitly; this default never follows it.
        ALTER TABLE embeddings ALTER COLUMN recipe SET DEFAULT '{BASELINE_RECIPE}';
        ALTER TABLE sightings ADD COLUMN vec_miew_model text,
            ADD COLUMN vec_miew_recipe text;
    """)


def downgrade():
    op.execute("""ALTER TABLE sightings DROP COLUMN vec_miew_recipe,
        DROP COLUMN vec_miew_model;
        ALTER TABLE embeddings DROP COLUMN recipe;""")
