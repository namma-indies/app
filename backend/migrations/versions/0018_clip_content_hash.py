"""An exact content hash on clips, so one video uploaded twice is one dog.

0016 hashed photos a contributor chose and deliberately left clip frames
unhashed: a frame is sampled from a clip, not picked. That left the clip itself
with no identity, and on 2026-10-24 sixteen of seventeen pending proposals were
one nine-frame clip uploaded six times, every copy asked about against every
other. The same file twice is the same answer as the same photo twice.

`captures.clip_sha256` is the sha256 of the clip as stored, which is the upload
byte for byte (both upload paths put the raw bytes to S3), so live rows and
backfilled rows mean the same thing. On the capture rather than the sighting
because every sighting has a capture (0015's trigger gives a /sighting upload
one) and a /capture clip can publish several sightings that all share it.

`auto_links.method` gains `identical_clip`, so a link made on a clip can be told
apart from one made on a photo when someone audits the rule.

Revision ID: 0018_clip_content_hash
Revises: 0017_social_picks
Create Date: 2026-10-24

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0018_clip_content_hash"
down_revision: Union[str, None] = "0017_social_picks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE captures ADD COLUMN clip_sha256 text;")
    op.execute(
        "CREATE INDEX ix_captures_clip_sha256 ON captures (clip_sha256) "
        "WHERE clip_sha256 IS NOT NULL;"
    )
    op.execute("ALTER TABLE auto_links DROP CONSTRAINT auto_links_method_check;")
    op.execute(
        "ALTER TABLE auto_links ADD CONSTRAINT auto_links_method_check "
        "CHECK (method IN ('identical_photo', 'identical_clip'));"
    )


def downgrade() -> None:
    op.execute("DELETE FROM auto_links WHERE method = 'identical_clip';")
    op.execute("ALTER TABLE auto_links DROP CONSTRAINT auto_links_method_check;")
    op.execute(
        "ALTER TABLE auto_links ADD CONSTRAINT auto_links_method_check "
        "CHECK (method IN ('identical_photo'));"
    )
    op.execute("DROP INDEX IF EXISTS ix_captures_clip_sha256;")
    op.execute("ALTER TABLE captures DROP COLUMN IF EXISTS clip_sha256;")
