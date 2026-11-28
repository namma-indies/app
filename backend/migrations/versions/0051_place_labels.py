"""Neighbourhood names for map cells.

Revision ID: 0051_place_labels
Revises: 0050_recognised

Akash, 2026-11-28: say where a sighting was in words ("Indiranagar,
Bengaluru"), not as "12.975, 77.639".

One row per map cell (the same grid `precision.coarsen` draws, so a name is
never sharper than what the map already shows anyone), with the name
OpenStreetMap gives that cell's centre. Rows are added the first time a
surface asks for a cell and filled in by a background worker at Nominatim's
one-request-a-second, so no request ever waits on the geocoder. `label` NULL
means not looked up yet, or nothing came back; `looked_up_at` says which, and
when to try again.

Not personal data: a cell centre and a place name, keyed by neither person
nor sighting. Downgrade drops the table.
"""
from alembic import op

revision = "0051_place_labels"
down_revision = "0050_recognised"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE place_labels (
            cell_m integer NOT NULL CHECK (cell_m > 0),
            lat double precision NOT NULL CHECK (lat BETWEEN -90 AND 90),
            lng double precision NOT NULL CHECK (lng BETWEEN -180 AND 180),
            label text CHECK (label IS NULL OR length(label) BETWEEN 1 AND 120),
            looked_up_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (cell_m, lat, lng)
        );
        -- The worker's question: what still needs a name.
        CREATE INDEX ix_place_labels_pending ON place_labels (looked_up_at NULLS FIRST, created_at)
            WHERE label IS NULL;
        GRANT SELECT, INSERT, UPDATE, DELETE ON place_labels TO app_rw;
    """)


def downgrade():
    op.execute("DROP TABLE place_labels")
