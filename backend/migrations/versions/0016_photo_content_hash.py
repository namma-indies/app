"""An exact content hash on photos, and a record of links no human made.

`photos.phash` is perceptual: it says two images look alike, which is what
frame selection needs and exactly what identity must not rest on -- two dogs on
the same street corner can share most of a phash. What the review queue was
actually full of is narrower and certain: the *same photograph* attached to two
sightings, picked twice from the gallery or replayed by the offline queue.
Of 33 pending proposals on 2026-10-23, 32 were that. A byte-identical photo is
the same dog (a product decision, not a threshold), so those pairs should
never have reached a person.

`content_sha256` is the sha256 of the stored original -- the EXIF-stripped
WebP that `process_photo` writes to S3 -- not of the bytes the phone sent.
That is deliberate: the upload itself is never kept, so a hash of it could not
be backfilled, and live rows and backfilled rows would then mean different
things in one column. The encode is deterministic (same pixels in, same bytes
out), so identical uploads still collide, and two files differing only in
metadata now collide too, which is the more accurate answer. The cost is that
an encoder upgrade starts a new hash space: a photo uploaded before it and
again after it will not match, and falls back to an ordinary proposal.

Only photos a contributor chose carry it: `POST /sighting` photos and
`/capture` sources. Clip frames and per-animal evidence crops are derived, and
the GPU worker publishes them by S3 copy, so the server never holds their
bytes. NULL for those means "not an uploaded photo", not "not yet hashed".

Not unique: duplicates are the point. Partial, because most rows that will ever
exist are crops and frames.

`auto_links` is the audit trail for identity links made by a rule rather than a
person. Deliberately NOT `confirmations`: every row there is a human verdict and
is the calibration set the re-ID thresholds will be fitted on, and a rule that
always says "same" would poison it with pairs nobody judged. One row per
sighting the rule linked, naming the sighting whose photo justified it, so a
bad link can be found and undone.

Revision ID: 0016_photo_content_hash
Revises: 0015_multi_animal_captures
Create Date: 2026-10-23

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0016_photo_content_hash"
down_revision: Union[str, None] = "0015_multi_animal_captures"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE photos ADD COLUMN content_sha256 text;")
    op.execute(
        "CREATE INDEX ix_photos_content_sha256 ON photos (content_sha256) "
        "WHERE content_sha256 IS NOT NULL;"
    )
    op.execute(
        """
        CREATE TABLE auto_links (
            id            uuid PRIMARY KEY,
            sighting_id   uuid NOT NULL REFERENCES sightings(id) ON DELETE CASCADE,
            individual_id uuid NOT NULL REFERENCES individuals(id),
            -- The other side of the evidence. SET NULL rather than CASCADE: the
            -- link outlives a deleted partner, and so should the record of it.
            matched_sighting_id uuid REFERENCES sightings(id) ON DELETE SET NULL,
            method        text NOT NULL CHECK (method IN ('identical_photo')),
            content_sha256 text,
            created_at    timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("CREATE INDEX ix_auto_links_sighting ON auto_links (sighting_id);")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON auto_links TO app_rw;")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auto_links;")
    op.execute("DROP INDEX IF EXISTS ix_photos_content_sha256;")
    op.execute("ALTER TABLE photos DROP COLUMN IF EXISTS content_sha256;")
