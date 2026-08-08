"""typed, indexable column for MiewID-msv3 embeddings

`embeddings.vec` is declared as bare `vector` (no dimension) so that one table
can hold several models at once. pgvector cannot build an HNSW or IVFFlat index
on a dimensionless column, so as written every candidate search would be a
sequential scan over the whole table. That is fine at pilot volume and a
problem well before we would notice it hurting.

We are committing to MiewID-msv3 (2152-d) as the first production embedder, so
this adds a dimension-typed column for it plus the ANN index, and leaves `vec`
in place for any other model we want to store unindexed (DINOv2-B is 768-d, so
it cannot share a typed column with MiewID). Rows keep the existing
UNIQUE (photo_id, model) contract; a MiewID row carries its vector in
`vec_miew` and leaves `vec` NULL.

Why a second column instead of retyping `vec` to vector(2152): retyping locks
the table to a single dimension forever, and our own benchmarks argue against
that bet. MiewID wins face crops at scale (96.6% top-1 on DogFaceNet, 1,336
identities) but lost to DINOv2 on full-body street photos (48 vs 30
misclassified pairs) -- and full-body street photos are exactly what IndieDex
collects. Keeping `vec` free means adding a second embedder later is another
column, not a migration of live vectors.

Index choice: HNSW over IVFFlat because it needs no training data and stays
accurate on a table that starts empty and grows continuously. Cosine ops to
match the embeddings, which are L2-normalised at write time.

The index is built on a `halfvec` cast, not on the column directly, because
pgvector's HNSW caps at **2000 dimensions** and MiewID-msv3 is **2152** --
indexing `vec_miew` directly fails outright with "column cannot have more than
2000 dimensions for hnsw index". `halfvec` raises that ceiling to 4000, so the
index stores fp16 copies while the column keeps full fp32 precision. That split
is the right shape anyway: half precision is ample for generating candidates,
and the exact score should be recomputed against the full-precision vector
before any merge decision is made.

Queries must use the same cast to hit the index, e.g.

    ORDER BY vec_miew::halfvec(2152) <=> $1::halfvec(2152) LIMIT 50

then re-rank those candidates on `vec_miew <=> $1` for the exact distance.

Revision ID: 0005_miewid_embedding_column
Revises: 0004_backfill_akash_magic_link
Create Date: 2026-08-08

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0005_miewid_embedding_column"
down_revision: Union[str, None] = "0004_backfill_akash_magic_link"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MIEWID_MODEL = "miewid-msv3"
MIEWID_DIM = 2152


def upgrade() -> None:
    op.execute(f"ALTER TABLE embeddings ADD COLUMN vec_miew vector({MIEWID_DIM});")

    # A MiewID row must actually carry a MiewID vector. Without this it is
    # possible to insert model='miewid-msv3' with both columns NULL, which
    # would silently drop the photo out of every candidate search while
    # looking like a successful embed.
    #
    # NOT VALID on purpose. Adding a validating CHECK scans every existing row,
    # and a violation aborts the migration -- which on this deployment means
    # uvicorn never starts and the site goes DOWN, because deploy/entrypoint.sh
    # runs `alembic upgrade head` before exec'ing the server and the old
    # container is already gone. NOT VALID enforces the rule on every new and
    # updated row while skipping that scan, so the deploy cannot be taken down
    # by pre-existing data. Validate later, at a time of your choosing:
    #     ALTER TABLE embeddings VALIDATE CONSTRAINT ck_embeddings_miew_vec;
    # which takes only a SHARE UPDATE EXCLUSIVE lock and does not block writes.
    op.execute(
        "ALTER TABLE embeddings ADD CONSTRAINT ck_embeddings_miew_vec "
        f"CHECK (model <> '{MIEWID_MODEL}' OR vec_miew IS NOT NULL) NOT VALID;"
    )

    # Built on a halfvec cast: HNSW refuses >2000 dimensions and MiewID is
    # 2152. Queries must use the identical cast expression to hit this index.
    op.execute(
        "CREATE INDEX ix_embeddings_vec_miew_hnsw ON embeddings "
        f"USING hnsw ((vec_miew::halfvec({MIEWID_DIM})) halfvec_cosine_ops);"
    )

    # Candidate search is always scoped to one model; this keeps that filter
    # cheap and is the partner of the vector index above.
    op.execute("CREATE INDEX ix_embeddings_model ON embeddings (model);")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_embeddings_model;")
    op.execute("DROP INDEX IF EXISTS ix_embeddings_vec_miew_hnsw;")
    op.execute(
        "ALTER TABLE embeddings DROP CONSTRAINT IF EXISTS ck_embeddings_miew_vec;"
    )
    op.execute("ALTER TABLE embeddings DROP COLUMN IF EXISTS vec_miew;")
