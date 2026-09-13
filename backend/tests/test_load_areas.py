"""Loading boundaries is a data operation, not a deploy.

The loader is generic over `kind` on purpose: loading PIN codes after wards is
the same script with different arguments. Re-running it on revised boundaries
must update in place, because a duplicate polygon is a double-counted dog.
"""

import pytest

from app.ids import uuid7
from scripts.load_areas import load_areas


def _square(x0, y0, size=1.0):
    x1, y1 = x0 + size, y0 + size
    return {
        "type": "Polygon",
        "coordinates": [[[x0, y0], [x0, y1], [x1, y1], [x1, y0], [x0, y0]]],
    }


def _feature(name, code, geom):
    return {"type": "Feature", "properties": {"nm": name, "cd": code}, "geometry": geom}


async def test_loads_features_as_areas_of_a_kind(migrated_db):
    result = await load_areas(
        migrated_db,
        features=[_feature("Domlur", "112", _square(77.6, 12.9))],
        kind="bbmp_ward",
        name_field="nm",
        code_field="cd",
    )
    assert result["inserted"] == 1
    row = await migrated_db.fetchrow("SELECT name, kind, ext_code FROM areas")
    assert (row["name"], row["kind"], row["ext_code"]) == ("Domlur", "bbmp_ward", "112")


async def test_reloading_updates_in_place(migrated_db):
    """Boundaries get revised. A second load must not double the map."""
    await load_areas(
        migrated_db,
        features=[_feature("Domlur", "112", _square(77.6, 12.9))],
        kind="bbmp_ward", name_field="nm", code_field="cd",
    )
    result = await load_areas(
        migrated_db,
        features=[_feature("Domlur Ward", "112", _square(77.6, 12.9, size=2.0))],
        kind="bbmp_ward", name_field="nm", code_field="cd",
    )
    assert result["inserted"] == 0
    assert result["updated"] == 1
    assert await migrated_db.fetchval("SELECT count(*) FROM areas") == 1
    assert await migrated_db.fetchval("SELECT name FROM areas") == "Domlur Ward"


async def test_polygon_is_coerced_to_multipolygon(migrated_db):
    """The column is MultiPolygon; real GeoJSON is a mix of both."""
    await load_areas(
        migrated_db,
        features=[_feature("A", "1", _square(0, 0))],
        kind="k", name_field="nm", code_field="cd",
    )
    kind = await migrated_db.fetchval("SELECT ST_GeometryType(geog::geometry) FROM areas")
    assert kind == "ST_MultiPolygon"


async def test_unsupported_geometry_is_rejected_not_skipped(migrated_db):
    """A Point in a boundary file means the wrong file or the wrong field.
    Storing nothing and reporting success would leave a silently empty map."""
    with pytest.raises(ValueError, match="Point"):
        await load_areas(
            migrated_db,
            features=[_feature("A", "1", {"type": "Point", "coordinates": [0, 0]})],
            kind="k", name_field="nm", code_field="cd",
        )


async def test_overlaps_within_a_kind_are_counted_and_reported(migrated_db):
    """Overlap makes attribution ambiguous. The read path stays correct
    regardless, so this warns rather than fails -- real administrative data
    has slivers and refusing to load it helps nobody."""
    result = await load_areas(
        migrated_db,
        features=[
            _feature("A", "1", _square(0, 0, size=2.0)),
            _feature("B", "2", _square(1, 1, size=2.0)),
        ],
        kind="k", name_field="nm", code_field="cd",
    )
    assert result["inserted"] == 2
    assert result["overlaps"] == 1


async def test_prune_removes_only_its_own_kind(migrated_db):
    await migrated_db.execute(
        "INSERT INTO areas (id, name, kind, ext_code, geog) VALUES ($1, 'keep', 'other', 'x', "
        "ST_GeogFromText('MULTIPOLYGON(((0 0, 0 1, 1 1, 1 0, 0 0)))'))",
        uuid7(),
    )
    await load_areas(
        migrated_db,
        features=[_feature("A", "1", _square(0, 0)), _feature("B", "2", _square(5, 5))],
        kind="k", name_field="nm", code_field="cd",
    )
    result = await load_areas(
        migrated_db,
        features=[_feature("A", "1", _square(0, 0))],
        kind="k", name_field="nm", code_field="cd", prune=True,
    )
    assert result["pruned"] == 1
    names = [r["name"] for r in await migrated_db.fetch("SELECT name FROM areas ORDER BY name")]
    assert names == ["A", "keep"]
