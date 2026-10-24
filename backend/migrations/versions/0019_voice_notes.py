"""A voice note kept with a sighting or a capture (issue #96).

An observer can speak about the dog when they log it ("she is asleep", "same
dog as last week"). For now the raw audio is only kept and linked; nothing
reads it yet. `audio_s3_key` follows `clip_s3_key`: the object sits under the
row's own prefix (`sightings/{id}/audio.<ext>` or `captures/{id}/audio.<ext>`)
so lifecycle rules and deletion treat it with the rest of the row's media.

On both tables because the two upload paths write different rows: /sighting
writes a sighting (its capture is a 'legacy' stand-in made by 0015's trigger),
/capture writes a capture whose sightings only exist after review. A clip's
own sound is its note, so a clip upload never carries this as well.

Revision ID: 0019_voice_notes
Revises: 0018_clip_content_hash
Create Date: 2026-10-24

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0019_voice_notes"
down_revision: Union[str, None] = "0018_clip_content_hash"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE sightings ADD COLUMN audio_s3_key text;")
    op.execute("ALTER TABLE captures ADD COLUMN audio_s3_key text;")


def downgrade() -> None:
    op.execute("ALTER TABLE captures DROP COLUMN IF EXISTS audio_s3_key;")
    op.execute("ALTER TABLE sightings DROP COLUMN IF EXISTS audio_s3_key;")
