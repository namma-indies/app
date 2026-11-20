"""Per-animal grouping confidence, and each animal's best view.

Revision ID: 0049_multi_animal_grouping
Revises: 0048_moderation_defaults

Akash, 2026-11-19: a capture whose grouping the worker is confident about
publishes on its own; an unsure one asks the person, with the best grouping
filled in (app/tracking.py).

* capture_tracks: one row per animal the worker grouped -- its voted species
  and whether that vote was close, how confident the grouping is and why
  not, and its representative box. Written once on completion, read by the
  review sheet and by `captures.recheck_held`. Gone with the capture.
* sightings.cover_photo_id: the evidence crop shown first for the animal
  (best confidence x size), instead of whichever crop sorted first. NULL for
  everything published before this and for legacy single-photo sightings.

Downgrade drops both; nothing else reads them.
"""
from alembic import op

revision = "0049_multi_animal_grouping"
down_revision = "0048_moderation_defaults"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE capture_tracks (
            capture_id uuid NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
            track_id text NOT NULL CHECK (length(track_id) BETWEEN 1 AND 64),
            species text NOT NULL CHECK (species IN ('dog','cat')),
            species_uncertain boolean NOT NULL,
            species_share real NOT NULL CHECK (species_share BETWEEN 0.5 AND 1),
            confidence real NOT NULL CHECK (confidence BETWEEN 0 AND 1),
            confident boolean NOT NULL,
            representative_instance_id uuid REFERENCES animal_instances(id) ON DELETE SET NULL,
            reasons jsonb NOT NULL DEFAULT '[]'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (capture_id, track_id)
        );
        ALTER TABLE sightings ADD COLUMN cover_photo_id uuid REFERENCES photos(id) ON DELETE SET NULL;
        GRANT SELECT, INSERT, UPDATE, DELETE ON capture_tracks TO app_rw;
    """)


def downgrade():
    op.execute("""
        ALTER TABLE sightings DROP COLUMN cover_photo_id;
        DROP TABLE capture_tracks;
    """)
