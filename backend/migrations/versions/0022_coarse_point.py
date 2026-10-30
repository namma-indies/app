"""`coarse_point(geog, cell_m)`: `app.precision.coarsen`, in SQL.

Some decisions about someone else's sighting are made inside a query rather
than in Python: which candidates are near enough to be offered as a match, and
which ward a sighting is counted in. Made on the exact point, either one turns
into a way of learning more about where the sighting is than the grid cell it
is shown as. Made on this, they depend on the cell alone.

Mirrors `coarsen` step for step -- the same metres-per-degree constant, the
longitude step taken from the snapped latitude, the same clamp on cos(lat), the
same rounding to five places -- and `tests/test_precision.py` checks the two
agree. IMMUTABLE so it can sit in a WHERE clause or an index expression.

Revision ID: 0022_coarse_point
Revises: 0021_detail_suggestions
Create Date: 2026-10-30

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0022_coarse_point"
down_revision: Union[str, None] = "0021_detail_suggestions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION coarse_point(g geography, cell_m double precision)
        RETURNS geography
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
        AS $$
            SELECT CASE WHEN cell_m <= 0 THEN g ELSE (
                SELECT ST_SetSRID(ST_MakePoint(
                    round(((floor(ST_X(g::geometry) / lng_step) + 0.5) * lng_step)::numeric, 5)::float8,
                    round(lat_c::numeric, 5)::float8
                ), 4326)::geography
                FROM (
                    SELECT lat_c,
                           cell_m / (111320.0 * greatest(cos(radians(lat_c)), 0.01)) AS lng_step
                    FROM (
                        SELECT (floor(ST_Y(g::geometry) / (cell_m / 111320.0)) + 0.5)
                               * (cell_m / 111320.0) AS lat_c
                    ) snapped_lat
                ) steps
            ) END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION coarse_point(geography, double precision);")
