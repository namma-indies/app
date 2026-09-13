"""`areas` is the coarsening target for every public count.

It shipped in 0001 with no spatial index and no way to tell one kind of
boundary from another, which is what 0008 fixes. The kind column is what
makes ward-vs-PIN-code a data question instead of a code change.
"""

import pytest


async def test_areas_has_kind_and_ext_code(migrated_db):
    cols = await migrated_db.fetch(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'areas'"
    )
    by_name = {r["column_name"]: r["is_nullable"] for r in cols}
    assert by_name["kind"] == "NO"
    assert by_name["ext_code"] == "YES"


async def test_areas_geog_has_a_spatial_index(migrated_db):
    """Without this every aggregate is a sequential scan over every polygon."""
    indexes = await migrated_db.fetch(
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'areas'"
    )
    defs = " ".join(r["indexdef"] for r in indexes)
    assert "using gist" in defs.lower()
    assert "geog" in defs


async def test_ext_code_is_unique_within_a_kind_only(migrated_db):
    """The same code may exist in two schemes; it may not repeat inside one.
    This is what makes the loader an upsert rather than a duplicator."""
    from app.ids import uuid7

    poly = "ST_GeogFromText('MULTIPOLYGON(((0 0, 0 1, 1 1, 1 0, 0 0)))')"
    await migrated_db.execute(
        f"INSERT INTO areas (id, name, kind, ext_code, geog) "
        f"VALUES ($1, 'A', 'bbmp_ward', '112', {poly})",
        uuid7(),
    )
    # Same code, different kind: allowed.
    await migrated_db.execute(
        f"INSERT INTO areas (id, name, kind, ext_code, geog) "
        f"VALUES ($1, 'B', 'pin_code', '112', {poly})",
        uuid7(),
    )
    with pytest.raises(Exception):
        await migrated_db.execute(
            f"INSERT INTO areas (id, name, kind, ext_code, geog) "
            f"VALUES ($1, 'C', 'bbmp_ward', '112', {poly})",
            uuid7(),
        )
