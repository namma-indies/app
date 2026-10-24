"""Sightings shared with the team, and the queue that posts one a day to X.

`social_picks` is both the queue and the history: a row is queued when a
moderator picks a photo and caption, becomes `posting` the moment the daily
run commits to it, and `posted` once X answers with an id. `passed` records a
"not this one" so the sighting leaves the shared list without being posted.

The rules the daily run must never break are constraints here rather than
checks in code, because a code check is exactly what a later change forgets:

- one row per sighting, so a sighting can be posted at most once;
- one row per photo, so two sightings cut from the same frame cannot both go;
- one `posted_on` per day, so a timer that fires twice -- `Persistent=true`
  after a reboot, plus the scheduled run -- still yields one post.

`posting` exists for the case where X accepted the post but the answer never
arrived. The row stays in that state until a person has looked at the
account; nothing reposts it automatically. See `app/social.py::run_daily`.

Handles and consent sit on `observers` and are set by hand for now (#92 builds
the screen). `social_consent` defaults to false so nobody is posted by default.

Revision ID: 0017_social_picks
Revises: 0016_photo_content_hash
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0017_social_picks"
down_revision: Union[str, None] = "0016_photo_content_hash"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE observers ADD COLUMN x_handle text, "
        "ADD COLUMN social_consent boolean NOT NULL DEFAULT false;"
    )
    op.execute("ALTER TABLE sightings ADD COLUMN shared_with_team_at timestamptz;")
    op.execute(
        "CREATE INDEX ix_sightings_shared_with_team ON sightings (shared_with_team_at) "
        "WHERE shared_with_team_at IS NOT NULL;"
    )
    op.execute(
        """
        CREATE TABLE social_picks (
            id uuid PRIMARY KEY,
            sighting_id uuid NOT NULL UNIQUE REFERENCES sightings(id) ON DELETE CASCADE,
            photo_id uuid REFERENCES photos(id) ON DELETE CASCADE,
            caption text,
            status text NOT NULL
                CHECK (status IN ('queued','posting','posted','passed')),
            picked_by uuid REFERENCES observers(id) ON DELETE SET NULL,
            picked_at timestamptz NOT NULL DEFAULT now(),
            posted_at timestamptz,
            posted_on date,
            x_post_id text,
            last_error text,
            last_attempt_at timestamptz,
            CHECK (status = 'passed' OR (photo_id IS NOT NULL AND caption IS NOT NULL))
        );
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_social_picks_photo ON social_picks (photo_id) "
        "WHERE photo_id IS NOT NULL;"
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_social_picks_posted_on ON social_picks (posted_on) "
        "WHERE posted_on IS NOT NULL;"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON social_picks TO app_rw;")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS social_picks;")
    op.execute("DROP INDEX IF EXISTS ix_sightings_shared_with_team;")
    op.execute("ALTER TABLE sightings DROP COLUMN IF EXISTS shared_with_team_at;")
    op.execute(
        "ALTER TABLE observers DROP COLUMN IF EXISTS social_consent, "
        "DROP COLUMN IF EXISTS x_handle;"
    )
