"""areas: distinguish one kind of boundary from another, and index the geometry

`areas` shipped in 0001 and has never been written to. It holds one column of
identity -- `name` -- which is enough for exactly one scheme of boundaries and
no more. Public counts need the scheme itself to be data: BBMP wards today,
PIN codes if the ward vintage turns out unusable, a hand-drawn pilot polygon
where neither fits. `kind` makes that a loader run and a config line rather
than a migration and a deploy, and lets two schemes coexist over one corpus.

`ext_code` is the source system's own identifier (ward number, PIN code). It
is what makes re-loading revised boundaries an update instead of a duplicate,
and it is the column somebody else's dataset joins to.

The GIST index is a straightforward omission from 0001: every aggregate query
asks ST_Covers against every polygon of a kind, which without it is a
sequential scan of the whole table per sighting.

The default on `kind` is added and immediately dropped rather than declaring
the column NOT NULL outright. `areas` is believed empty on every environment,
but `deploy/entrypoint.sh` is `set -e` -- a migration that aborts does not
leave the site un-updated, it leaves the site down, with the old container
already gone. This form is correct whether or not the belief holds.

Revision ID: 0008_areas_kind
Revises: 0007_geo_source_exif
Create Date: 2026-09-13

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0008_areas_kind"
down_revision: Union[str, None] = "0007_geo_source_exif"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE areas ADD COLUMN kind text NOT NULL DEFAULT 'unknown';")
    op.execute("ALTER TABLE areas ALTER COLUMN kind DROP DEFAULT;")
    op.execute("ALTER TABLE areas ADD COLUMN ext_code text;")
    # Partial: a scheme without stable external codes (hand-drawn pilot
    # polygons) may legitimately leave ext_code NULL on every row, and NULLs
    # would not collide anyway -- stating it keeps the intent readable.
    op.execute(
        "CREATE UNIQUE INDEX ux_areas_kind_ext_code ON areas (kind, ext_code) "
        "WHERE ext_code IS NOT NULL;"
    )
    op.execute("CREATE INDEX ix_areas_kind ON areas (kind);")
    op.execute("CREATE INDEX ix_areas_geog ON areas USING GIST (geog);")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_areas_geog;")
    op.execute("DROP INDEX IF EXISTS ix_areas_kind;")
    op.execute("DROP INDEX IF EXISTS ux_areas_kind_ext_code;")
    op.execute("ALTER TABLE areas DROP COLUMN IF EXISTS ext_code;")
    op.execute("ALTER TABLE areas DROP COLUMN IF EXISTS kind;")
