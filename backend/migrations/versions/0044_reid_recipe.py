"""Tag vectors and cached means with their re-ID recipe (#221).

Existing per-photo rows for the current model are the baseline recipe. Existing
means deliberately remain untagged: rebuild them from those frames with the
backfill, never assert their provenance by attaching a tag to an old cache.

Metadata-only on purpose. `embeddings` carries an HNSW index, and an UPDATE of
every row would rewrite the table and churn the index under an exclusive lock.
Instead the column is added with a constant default (Postgres 11+ stores it in
the catalogue, no rewrite), which tags every existing row with the baseline,
and the default is dropped again at once: from here on a writer must say which
recipe produced a vector. Only rows that are not baseline vectors -- another
model, or no vector at all -- are touched, to take the tag off; there should
be none or few.

`embeddings_recipe_tagged`: a MiewID vector without a recipe is refused, so an
untagged insert fails loudly instead of slipping out of every active-recipe
query unnoticed.
"""

from alembic import op

revision = "0044_reid_recipe"
down_revision = "0043_model_screening"
branch_labels = None
depends_on = None

BASELINE_RECIPE = "stored-webp90-yolo640-miew440-imagenet-l2-mean-v1"
MODEL = "miewid-msv3"


def upgrade():
    op.execute(f"""
        ALTER TABLE embeddings ADD COLUMN recipe text DEFAULT '{BASELINE_RECIPE}';
        ALTER TABLE embeddings ALTER COLUMN recipe DROP DEFAULT;
        UPDATE embeddings SET recipe = NULL WHERE model <> '{MODEL}' OR vec_miew IS NULL;
        -- NOT VALID, like ck_embeddings_miew_vec (0005): every existing row
        -- satisfies it by construction, so the scan would only lengthen the
        -- exclusive lock. New and updated rows are checked.
        ALTER TABLE embeddings ADD CONSTRAINT embeddings_recipe_tagged
            CHECK (model <> '{MODEL}' OR vec_miew IS NULL OR recipe IS NOT NULL) NOT VALID;
        ALTER TABLE sightings ADD COLUMN vec_miew_model text,
            ADD COLUMN vec_miew_recipe text;
    """)


def downgrade():
    op.execute("""ALTER TABLE sightings DROP COLUMN vec_miew_recipe,
        DROP COLUMN vec_miew_model;
        ALTER TABLE embeddings DROP CONSTRAINT IF EXISTS embeddings_recipe_tagged;
        ALTER TABLE embeddings DROP COLUMN recipe;""")
