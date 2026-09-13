# Aggregate API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship IndieDex's first read API — how many people have seen how many dogs, city-wide and per area — behind the existing cohort gate, with area defined as pluggable data rather than hardcoded, plus basic rate limiting.

**Architecture:** One module owns every derived count and the single definition of a countable sighting (`app/aggregates.py`); a thin router exposes three endpoints over it (`app/routes/stats.py`); a separate in-process fixed-window limiter (`app/ratelimit.py`) is applied per-route to both the new surface and the untouched auth surface. Areas become rows distinguished by `kind`, loaded by a generic script, so ward-vs-PIN-code is a config line rather than a code change.

**Tech Stack:** FastAPI, asyncpg, PostGIS, Alembic, pytest (`asyncio_mode = "auto"`), uv.

**Spec:** `docs/specs/2026-09-13-aggregate-api-design.md`

## Global Constraints

- **Countable sighting is defined exactly once**, in `app/aggregates.py`, as `s.review_status <> 'rejected'`. Never re-inline it.
- **Month is the finest time grain that exists.** No daily or weekly bucket may be added to any query or response.
- **Months bucket in `Asia/Kolkata`**, via `date_trunc('month', s.captured_at AT TIME ZONE 'Asia/Kolkata')`. `captured_at` is `timestamptz`; bucketing in UTC would file an 11pm IST sighting under the previous month.
- **Area attribution always uses the lateral join with `LIMIT 1`.** A plain join double-counts when polygons overlap.
- **Suppressed areas are omitted, never flagged.** A suppressed area fetched by id returns 404, identical to a nonexistent id.
- **Every aggregate response carries `areas_suppressed` and `unattributed_sightings`** so a reader can reconcile per-area rows against the city total.
- Tests run against real Postgres via the existing `migrated_db` / `app_client` / `authed_client` fixtures in `backend/tests/conftest.py`.
- All backend commands run from `backend/` with `uv run`.
- Commit after every task. Never commit with a failing test.

---

### Task 1: Migration — `areas` gains a kind

**Files:**
- Create: `backend/migrations/versions/0008_areas_kind.py`
- Test: `backend/tests/test_areas_schema.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `areas.kind text NOT NULL`, `areas.ext_code text`, unique index on `(kind, ext_code)` where `ext_code IS NOT NULL`, GIST index on `areas.geog`, btree on `areas.kind`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_areas_schema.py`:

```python
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
    assert "USING gist" in defs.lower()
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_areas_schema.py -v`
Expected: FAIL — `KeyError: 'kind'`.

- [ ] **Step 3: Write the migration**

Create `backend/migrations/versions/0008_areas_kind.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/test_areas_schema.py -v`
Expected: 3 passed.

- [ ] **Step 5: Verify the migration reverses cleanly**

Run: `cd backend && ALEMBIC_DB_URL="$(uv run python -c 'from app.config import settings; print(settings.test_database_url_sync)')" uv run alembic downgrade 0007_geo_source_exif && ALEMBIC_DB_URL="$(uv run python -c 'from app.config import settings; print(settings.test_database_url_sync)')" uv run alembic upgrade head`
Expected: both complete without error.

- [ ] **Step 6: Add `areas` to the test truncation list**

`backend/tests/conftest.py` TRUNCATEs a fixed list of tables between tests and
`areas` is not on it — so polygons inserted by one test would survive into the
next and silently change its counts. Add `areas` to the list in **both**
places (the `migrated_db` fixture and the `app_client` fixture):

```python
            "TRUNCATE observers, sightings, photos, embeddings, individuals, "
            "match_proposals, confirmations, clinical_records, login_tokens, "
            "areas RESTART IDENTITY CASCADE"
```

- [ ] **Step 7: Confirm isolation actually holds**

Run: `cd backend && uv run pytest tests/test_areas_schema.py -v -p no:randomly`
then run it a second time. Expected: 3 passed both times — the unique-index
test inserts fixed codes and would fail on a second run if truncation missed.

- [ ] **Step 8: Commit**

```bash
git add backend/migrations/versions/0008_areas_kind.py backend/tests/test_areas_schema.py backend/tests/conftest.py
git commit -m "feat(areas): make the kind of boundary a property of the data"
```

---

### Task 2: The area loader

**Files:**
- Create: `backend/scripts/load_areas.py`
- Test: `backend/tests/test_load_areas.py`

**Interfaces:**
- Consumes: `areas.kind` / `areas.ext_code` from Task 1.
- Produces: `async def load_areas(conn, *, features: list[dict], kind: str, name_field: str, code_field: str | None, prune: bool = False) -> dict` returning `{"inserted": int, "updated": int, "pruned": int, "overlaps": int}`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_load_areas.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_load_areas.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.load_areas'`.

- [ ] **Step 3: Write the loader**

Create `backend/scripts/load_areas.py`:

```python
"""Load boundary polygons into `areas` as a named kind.

Generic over `kind` by design: BBMP wards today, PIN codes if the ward vintage
turns out unusable, neighbourhood outlines or hand-drawn pilot polygons later.
Loading a second scheme is this script with different arguments, not new code.

    uv run python scripts/load_areas.py \
        --geojson bbmp-wards.geojson \
        --kind bbmp_ward \
        --name-field KGISWardName \
        --code-field KGISWardNo

The GeoJSON is deliberately not committed: it is public data, but
full-resolution ward polygons are megabytes in a repo whose value is the code.
Record the source URL, the vintage of the boundaries, and the exact invocation
in the ops repo -- a boundary set whose provenance is unknown cannot be
audited later, and the Bangalore ward list is a live question rather than a
lookup.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.ids import uuid7  # noqa: E402

SUPPORTED = ("Polygon", "MultiPolygon")


def _to_multipolygon(geom: dict) -> dict:
    """The column is MultiPolygon; real boundary files mix both types.

    Rejecting anything else rather than skipping it: a Point or LineString in
    a boundary file means the wrong file or the wrong layer, and a loader that
    reports success over an empty map is worse than one that stops.
    """
    kind = geom.get("type")
    if kind == "MultiPolygon":
        return geom
    if kind == "Polygon":
        return {"type": "MultiPolygon", "coordinates": [geom["coordinates"]]}
    raise ValueError(f"unsupported geometry type {kind!r}; expected one of {SUPPORTED}")


async def load_areas(
    conn,
    *,
    features: list[dict],
    kind: str,
    name_field: str,
    code_field: str | None,
    prune: bool = False,
) -> dict:
    inserted = updated = pruned = 0
    seen: list[str] = []

    for feature in features:
        props = feature.get("properties") or {}
        name = props.get(name_field)
        code = str(props[code_field]) if code_field and props.get(code_field) is not None else None
        geom = json.dumps(_to_multipolygon(feature["geometry"]))

        if code is not None:
            seen.append(code)
            # ST_Multi guards against a GeoJSON MultiPolygon that Postgres
            # narrows to Polygon when it holds a single ring.
            row = await conn.fetchrow(
                """
                INSERT INTO areas (id, name, kind, ext_code, geog)
                VALUES ($1, $2, $3, $4, ST_Multi(ST_GeomFromGeoJSON($5))::geography)
                ON CONFLICT (kind, ext_code) WHERE ext_code IS NOT NULL
                DO UPDATE SET name = EXCLUDED.name, geog = EXCLUDED.geog
                RETURNING (xmax = 0) AS was_insert
                """,
                uuid7(), name, kind, code, geom,
            )
            if row["was_insert"]:
                inserted += 1
            else:
                updated += 1
        else:
            await conn.execute(
                "INSERT INTO areas (id, name, kind, ext_code, geog) "
                "VALUES ($1, $2, $3, NULL, ST_Multi(ST_GeomFromGeoJSON($4))::geography)",
                uuid7(), name, kind, geom,
            )
            inserted += 1

    if prune and code_field:
        pruned = await conn.fetchval(
            "WITH gone AS (DELETE FROM areas WHERE kind = $1 AND ext_code IS NOT NULL "
            "AND NOT (ext_code = ANY($2::text[])) RETURNING 1) SELECT count(*) FROM gone",
            kind, seen,
        )

    # Overlap makes attribution ambiguous: a sighting inside two polygons of
    # one kind has to be assigned to one of them. The read path already makes
    # that deterministic and never double-counts, so this is reported rather
    # than enforced -- real administrative boundaries carry slivers, and
    # refusing to load them would help nobody.
    overlaps = await conn.fetchval(
        "SELECT count(*) FROM areas a JOIN areas b "
        "ON a.kind = b.kind AND a.id < b.id AND ST_Overlaps(a.geog::geometry, b.geog::geometry) "
        "WHERE a.kind = $1",
        kind,
    )
    return {"inserted": inserted, "updated": updated, "pruned": pruned, "overlaps": overlaps}


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--geojson", required=True, type=Path)
    parser.add_argument("--kind", required=True)
    parser.add_argument("--name-field", required=True)
    parser.add_argument("--code-field")
    parser.add_argument("--prune", action="store_true",
                        help="delete rows of this kind absent from the file")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    doc = json.loads(args.geojson.read_text())
    features = doc["features"] if doc.get("type") == "FeatureCollection" else [doc]
    print(f"{len(features)} features in {args.geojson}")
    if args.dry_run:
        for feature in features[:5]:
            print("  ", (feature.get("properties") or {}).get(args.name_field))
        return

    conn = await asyncpg.connect(settings.database_url)
    try:
        async with conn.transaction():
            result = await load_areas(
                conn, features=features, kind=args.kind,
                name_field=args.name_field, code_field=args.code_field,
                prune=args.prune,
            )
    finally:
        await conn.close()
    print(result)
    if result["overlaps"]:
        print(f"!! {result['overlaps']} overlapping pairs in kind {args.kind!r} -- "
              "attribution for sightings in the overlap is arbitrary but stable")


if __name__ == "__main__":
    asyncio.run(_main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/test_load_areas.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/scripts/load_areas.py backend/tests/test_load_areas.py
git commit -m "feat(areas): load boundary polygons of any kind, idempotently"
```

---

### Task 3: The aggregate layer

**Files:**
- Create: `backend/app/aggregates.py`
- Test: `backend/tests/test_aggregates.py`

**Interfaces:**
- Consumes: `areas.kind` (Task 1).
- Produces, all importable from `app.aggregates`:
  - `COUNTABLE_SIGHTING: str`
  - `def thresholds(kind: str) -> tuple[int, int]` — `(min_sightings, min_observers)`
  - `async def city_totals(conn) -> dict` — keys `observers`, `sightings`, `confirmed_individuals`
  - `async def city_months(conn) -> list[dict]` — keys `month`, `observers`, `sightings`, `confirmed_individuals`
  - `async def area_rows(conn, kind: str) -> list[dict]` — unsuppressed; keys `id`, `name`, `ext_code`, `sightings`, `confirmed_individuals`, `observers`, `last_active_month`
  - `async def unattributed_sightings(conn, kind: str) -> int`
  - `async def area_months(conn, area_id: UUID, kind: str) -> list[dict]`
  - `def suppress(rows: list[dict], kind: str) -> tuple[list[dict], int]` — returns `(kept, suppressed_count)`

- [ ] **Step 1: Add the config knobs**

Modify `backend/app/config.py` — add after the `reid_max_candidates` block:

```python
    # --- aggregates --------------------------------------------------------
    # Which boundary scheme a public count is labelled with when the caller
    # does not say. A data question, not a code one: falling back from wards
    # to PIN codes is this line plus a loader run.
    area_default_kind: str = "bbmp_ward"

    # Small-cell suppression. An area is reported only if it clears BOTH.
    # Per kind, because the threshold protects a privacy property and that
    # property depends on cell size: a Bangalore PIN code and a BBMP ward
    # differ by roughly an order of magnitude in area, so one number cannot be
    # right for both. A global threshold would also silently become the wrong
    # number the moment the PIN-code fallback happened, with no code change to
    # notice it. JSON in the environment: AREA_MIN_SIGHTINGS='{"bbmp_ward":5}'
    area_min_sightings: dict[str, int] = {"bbmp_ward": 5, "pin_code": 20}
    area_min_observers: dict[str, int] = {"bbmp_ward": 2, "pin_code": 3}
    # Applied to any kind not named above, including one loaded tomorrow.
    # Deliberately the stricter of the two configured pairs is not used here;
    # an unknown kind gets the ward defaults and should be given its own entry
    # before it is published.
    area_min_sightings_default: int = 5
    area_min_observers_default: int = 2
```

- [ ] **Step 2: Write the failing test**

Create `backend/tests/test_aggregates.py`:

```python
"""Derived counts, and the rules that keep them from becoming a location.

Three properties are load-bearing and each has a test here:
  - a sighting is counted once, even where polygons overlap;
  - a thin area is omitted entirely rather than flagged, because a flag still
    says "a dog is here";
  - the numbers reconcile -- per-area rows plus suppressed plus unattributed
    account for the city total.
"""

from datetime import datetime, timezone

from app.aggregates import (
    area_months,
    area_rows,
    city_months,
    city_totals,
    suppress,
    thresholds,
    unattributed_sightings,
)
from app.ids import uuid7


async def _observer(conn, name="o"):
    oid = uuid7()
    await conn.execute(
        "INSERT INTO observers (id, display_name) VALUES ($1, $2)", oid, name
    )
    return oid


async def _area(conn, name, kind, code, x0, y0, size=1.0):
    aid = uuid7()
    x1, y1 = x0 + size, y0 + size
    await conn.execute(
        f"INSERT INTO areas (id, name, kind, ext_code, geog) VALUES ($1, $2, $3, $4, "
        f"ST_GeogFromText('MULTIPOLYGON((({x0} {y0}, {x0} {y1}, {x1} {y1}, "
        f"{x1} {y0}, {x0} {y0})))'))",
        aid, name, kind, code,
    )
    return aid


async def _sighting(conn, observer, *, lat=None, lng=None, when="2026-08-15",
                    individual=None, review="valid"):
    sid = uuid7()
    geog = "NULL" if lat is None else f"ST_GeogFromText('POINT({lng} {lat})')"
    await conn.execute(
        f"INSERT INTO sightings (id, observer_id, captured_at, geog, individual_id, "
        f"review_status) VALUES ($1, $2, $3, {geog}, $4, $5)",
        sid, observer,
        datetime.fromisoformat(when).replace(tzinfo=timezone.utc),
        individual, review,
    )
    return sid


async def _individual(conn):
    iid = uuid7()
    await conn.execute("INSERT INTO individuals (id) VALUES ($1)", iid)
    return iid


async def test_three_numbers_are_counted_separately(migrated_db):
    """Sightings overcount dogs, confirmed individuals undercount them. The
    API returns both rather than one guess wearing a fact's clothes."""
    o1 = await _observer(migrated_db, "a")
    o2 = await _observer(migrated_db, "b")
    dog = await _individual(migrated_db)
    await _sighting(migrated_db, o1, lat=12.9, lng=77.6, individual=dog)
    await _sighting(migrated_db, o1, lat=12.9, lng=77.6, individual=dog)
    await _sighting(migrated_db, o2, lat=12.9, lng=77.6)  # unmatched

    totals = await city_totals(migrated_db)
    assert totals == {"observers": 2, "sightings": 3, "confirmed_individuals": 1}


async def test_rejected_sightings_count_nowhere(migrated_db):
    o = await _observer(migrated_db)
    await _sighting(migrated_db, o, lat=12.9, lng=77.6)
    await _sighting(migrated_db, o, lat=12.9, lng=77.6, review="rejected")
    assert (await city_totals(migrated_db))["sightings"] == 1


async def test_a_sighting_in_two_overlapping_areas_is_counted_once(migrated_db):
    """Without LIMIT 1 in the lateral join, per-area rows exceed the city
    total and the map appears to grow dogs."""
    o = await _observer(migrated_db)
    await _area(migrated_db, "A", "k", "1", 77.0, 12.0, size=2.0)
    await _area(migrated_db, "B", "k", "2", 77.5, 12.5, size=2.0)
    await _sighting(migrated_db, o, lat=13.0, lng=77.8)  # inside both

    rows = await area_rows(migrated_db, "k")
    assert sum(r["sightings"] for r in rows) == 1


async def test_sightings_outside_every_polygon_are_unattributed(migrated_db):
    o = await _observer(migrated_db)
    await _area(migrated_db, "A", "k", "1", 77.0, 12.0)
    await _sighting(migrated_db, o, lat=12.5, lng=77.5)   # inside
    await _sighting(migrated_db, o, lat=50.0, lng=10.0)   # outside every polygon
    await _sighting(migrated_db, o, lat=None, lng=None)   # geo_source 'none'

    assert await unattributed_sightings(migrated_db, "k") == 2


async def test_kinds_do_not_leak_into_each_other(migrated_db):
    o = await _observer(migrated_db)
    await _area(migrated_db, "Ward", "bbmp_ward", "1", 77.0, 12.0)
    await _area(migrated_db, "Pin", "pin_code", "560038", 77.0, 12.0)
    await _sighting(migrated_db, o, lat=12.5, lng=77.5)

    wards = await area_rows(migrated_db, "bbmp_ward")
    pins = await area_rows(migrated_db, "pin_code")
    assert [r["name"] for r in wards] == ["Ward"]
    assert [r["name"] for r in pins] == ["Pin"]
    assert wards[0]["sightings"] == pins[0]["sightings"] == 1


async def test_months_bucket_in_india_not_utc(migrated_db):
    """23:30 UTC on 31 August is 05:00 IST on 1 September. A Bangalore
    project that files that under August is reporting someone else's month."""
    o = await _observer(migrated_db)
    await _sighting(migrated_db, o, lat=12.9, lng=77.6, when="2026-08-31T23:30:00")
    months = await city_months(migrated_db)
    assert [m["month"] for m in months] == ["2026-09"]


async def test_nothing_finer_than_a_month_is_emitted(migrated_db):
    o = await _observer(migrated_db)
    await _sighting(migrated_db, o, lat=12.9, lng=77.6, when="2026-07-02")
    await _sighting(migrated_db, o, lat=12.9, lng=77.6, when="2026-07-20")
    await _sighting(migrated_db, o, lat=12.9, lng=77.6, when="2026-08-03")
    months = await city_months(migrated_db)
    assert [m["month"] for m in months] == ["2026-07", "2026-08"]
    assert months[0]["sightings"] == 2


async def test_suppression_needs_both_thresholds(migrated_db):
    """Plenty of sightings from a single observer is still one person's
    routine, which is the pattern the three dials exist to strip."""
    rows = [
        {"name": "thin", "sightings": 4, "observers": 3},
        {"name": "lonely", "sightings": 40, "observers": 1},
        {"name": "ok", "sightings": 5, "observers": 2},
    ]
    kept, suppressed = suppress(rows, "bbmp_ward")
    assert [r["name"] for r in kept] == ["ok"]
    assert suppressed == 2


async def test_unknown_kinds_fall_back_to_a_threshold(migrated_db):
    assert thresholds("something_new") == (5, 2)
    assert thresholds("pin_code") == (20, 3)


async def test_an_areas_month_series_matches_its_own_attribution(migrated_db):
    """The series must use the same lateral attribution as the area row, or
    an overlapping polygon gives an area months it was not credited with."""
    o = await _observer(migrated_db)
    a = await _area(migrated_db, "A", "k", "1", 77.0, 12.0, size=2.0)
    await _area(migrated_db, "B", "k", "2", 77.5, 12.5, size=2.0)
    await _sighting(migrated_db, o, lat=13.0, lng=77.8, when="2026-07-10")

    a_months = await area_months(migrated_db, a, "k")
    assert sum(m["sightings"] for m in a_months) == 1
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_aggregates.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.aggregates'`.

- [ ] **Step 4: Write the module**

Create `backend/app/aggregates.py`:

```python
"""Derived counts over the sighting corpus.

This module owns two things nothing else may duplicate: what counts as a
sighting, and how a sighting is attributed to an area. Both were previously
inlined per endpoint, which is how `/dex` came to disagree with `/map` about
rejected sightings (issue #54).

Everything here returns counts. No row, coordinate or photo leaves this
module, which is what makes the surface above it safe to open to people who
are not signed in -- see docs/specs/2026-09-13-aggregate-api-design.md.
"""

from uuid import UUID

from app.config import settings

# The one definition. `/map` and `/dogs` import it rather than restating it.
COUNTABLE_SIGHTING = "s.review_status <> 'rejected'"

# Bangalore. `captured_at` is timestamptz and date_trunc would otherwise bucket
# in whatever the session timezone is; 23:30 UTC on the 31st is the 1st here.
_MONTH = "date_trunc('month', s.captured_at AT TIME ZONE 'Asia/Kolkata')"

# At most one area per sighting, per kind.
#
# LIMIT 1 is load-bearing rather than defensive. Administrative boundaries
# overlap in practice, and a plain join credits an overlapping sighting to
# every polygon that covers it -- so the per-area rows exceed the city total
# and the data appears to have grown dogs. ORDER BY a.id makes the choice
# arbitrary but stable, so a sighting does not move between areas per request.
#
# A sighting with a NULL geog produces no rows here (ST_Covers against NULL is
# NULL), so it falls out as unattributed without a special case.
_AREA_LATERAL = """
    LEFT JOIN LATERAL (
        SELECT a.id, a.name, a.ext_code
        FROM areas a
        WHERE a.kind = $1 AND ST_Covers(a.geog, s.geog)
        ORDER BY a.id
        LIMIT 1
    ) a ON TRUE
"""


def thresholds(kind: str) -> tuple[int, int]:
    """(min_sightings, min_observers) below which an area is not reported."""
    return (
        settings.area_min_sightings.get(kind, settings.area_min_sightings_default),
        settings.area_min_observers.get(kind, settings.area_min_observers_default),
    )


def suppress(rows: list[dict], kind: str) -> tuple[list[dict], int]:
    """Drop areas too thin to report, and say how many were dropped.

    Dropped entirely rather than returned with null counts and a flag: a flag
    still discloses "at least one dog is here", which is the presence fact the
    whole exercise protects. The count is returned so a reader can still
    reconcile the per-area rows against the city total.
    """
    min_sightings, min_observers = thresholds(kind)
    kept = [
        r for r in rows
        if r["sightings"] >= min_sightings and r["observers"] >= min_observers
    ]
    return kept, len(rows) - len(kept)


async def city_totals(conn) -> dict:
    row = await conn.fetchrow(
        f"""
        SELECT
            COUNT(DISTINCT s.observer_id) AS observers,
            COUNT(*) AS sightings,
            COUNT(DISTINCT s.individual_id) AS confirmed_individuals
        FROM sightings s
        WHERE {COUNTABLE_SIGHTING}
        """
    )
    return dict(row)


async def city_months(conn) -> list[dict]:
    rows = await conn.fetch(
        f"""
        SELECT
            to_char({_MONTH}, 'YYYY-MM') AS month,
            COUNT(DISTINCT s.observer_id) AS observers,
            COUNT(*) AS sightings,
            COUNT(DISTINCT s.individual_id) AS confirmed_individuals
        FROM sightings s
        WHERE {COUNTABLE_SIGHTING}
        GROUP BY 1
        ORDER BY 1
        """
    )
    return [dict(r) for r in rows]


async def area_rows(conn, kind: str) -> list[dict]:
    """Every area of `kind` with at least one countable sighting, unsuppressed.

    Suppression is applied by the caller so that the number of dropped areas
    can be reported alongside what survived.
    """
    rows = await conn.fetch(
        f"""
        SELECT
            a.id, a.name, a.ext_code,
            COUNT(*) AS sightings,
            COUNT(DISTINCT s.individual_id) AS confirmed_individuals,
            COUNT(DISTINCT s.observer_id) AS observers,
            to_char(MAX({_MONTH}), 'YYYY-MM') AS last_active_month
        FROM sightings s
        {_AREA_LATERAL}
        WHERE {COUNTABLE_SIGHTING} AND a.id IS NOT NULL
        GROUP BY a.id, a.name, a.ext_code
        ORDER BY a.name
        """,
        kind,
    )
    return [dict(r) for r in rows]


async def unattributed_sightings(conn, kind: str) -> int:
    """Countable sightings this kind cannot place: no location recorded
    (`geo_source` allows 'none'), or outside every polygon loaded.

    Reported rather than quietly dropped -- without it the per-area rows do
    not add up to the city total and the difference looks like missing dogs.
    """
    return await conn.fetchval(
        f"""
        SELECT COUNT(*)
        FROM sightings s
        {_AREA_LATERAL}
        WHERE {COUNTABLE_SIGHTING} AND a.id IS NULL
        """,
        kind,
    )


async def area_months(conn, area_id: UUID, kind: str) -> list[dict]:
    """One area's monthly series, attributed exactly as its row was.

    Re-uses the lateral join rather than joining straight to the one area: a
    direct join would credit this area with sightings that overlap it but were
    attributed elsewhere, so the series would disagree with the row above it.
    """
    rows = await conn.fetch(
        f"""
        SELECT
            to_char({_MONTH}, 'YYYY-MM') AS month,
            COUNT(*) AS sightings,
            COUNT(DISTINCT s.individual_id) AS confirmed_individuals,
            COUNT(DISTINCT s.observer_id) AS observers
        FROM sightings s
        {_AREA_LATERAL}
        WHERE {COUNTABLE_SIGHTING} AND a.id = $2
        GROUP BY 1
        ORDER BY 1
        """,
        kind, area_id,
    )
    return [dict(r) for r in rows]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/test_aggregates.py -v`
Expected: 10 passed.

- [ ] **Step 6: Repoint the two endpoints that already inline the predicate**

Modify `backend/app/routes/map.py` — replace `AND s.review_status <> 'rejected'` in the SQL with an f-string interpolation of the shared constant, and add the import:

```python
from app.aggregates import COUNTABLE_SIGHTING
```

The `WHERE` clause becomes:

```python
    sql = f"""
        SELECT DISTINCT ON (s.id)
```
...
```python
        WHERE s.geog IS NOT NULL
          AND {COUNTABLE_SIGHTING}
    """
```

Do the same in `backend/app/routes/dogs.py` at its `review_status <> 'rejected'` filter.

**Do not touch `backend/app/routes/dex.py`.** It omits this filter, which issue #54 identifies as a gap — but whether your own rejected sighting disappears from your own Journal is a product question that belongs to #54, not a silent side effect of this change.

- [ ] **Step 7: Run the full suite to confirm the refactor changed no behaviour**

Run: `cd backend && uv run pytest -q`
Expected: all pass, including the pre-existing `test_map.py` and `test_dogs_route.py`.

- [ ] **Step 8: Commit**

```bash
git add backend/app/aggregates.py backend/tests/test_aggregates.py backend/app/config.py backend/app/routes/map.py backend/app/routes/dogs.py
git commit -m "feat(aggregates): count the corpus without ever naming a place"
```

---

### Task 4: The endpoints

**Files:**
- Create: `backend/app/routes/stats.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_stats.py`

**Interfaces:**
- Consumes: every function from `app.aggregates` (Task 3), `require_observer`, `get_conn`.
- Produces: `router` with `GET /stats`, `GET /stats/areas`, `GET /stats/areas/{area_id}`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_stats.py`:

```python
"""The first surface a stranger could safely read.

Cohort-gated today. The properties tested here are the ones that have to hold
before that gate comes off: a thin area is indistinguishable from one that
does not exist, and the numbers reconcile in public.
"""

from datetime import datetime, timezone

from app.ids import uuid7


async def _pool(client):
    """Seed through the app's own pool, never via the `migrated_db` fixture.

    `migrated_db` TRUNCATEs on setup and so does `app_client`. A test that
    asks for both gets `migrated_db`'s truncation *after* `authed_client` has
    inserted the observer whose session it is using -- deleting that observer,
    and turning every subsequent request into a 401 that looks like an auth
    bug. One connection source per test removes the ordering question.
    """
    return client._transport.app.state.pool


async def _seed(client, *, areas=(), sightings=()):
    pool = await _pool(client)
    async with pool.acquire() as conn:
        for name, kind, code, x0, y0, size in areas:
            x1, y1 = x0 + size, y0 + size
            await conn.execute(
                f"INSERT INTO areas (id, name, kind, ext_code, geog) VALUES ($1, $2, $3, $4, "
                f"ST_GeogFromText('MULTIPOLYGON((({x0} {y0}, {x0} {y1}, {x1} {y1}, "
                f"{x1} {y0}, {x0} {y0})))'))",
                uuid7(), name, kind, code,
            )
        for observer_name, lat, lng, when in sightings:
            oid = await conn.fetchval(
                "SELECT id FROM observers WHERE display_name = $1", observer_name
            )
            if oid is None:
                oid = uuid7()
                await conn.execute(
                    "INSERT INTO observers (id, display_name) VALUES ($1, $2)",
                    oid, observer_name,
                )
            geog = "NULL" if lat is None else f"ST_GeogFromText('POINT({lng} {lat})')"
            await conn.execute(
                f"INSERT INTO sightings (id, observer_id, captured_at, geog) "
                f"VALUES ($1, $2, $3, {geog})",
                uuid7(), oid,
                datetime.fromisoformat(when).replace(tzinfo=timezone.utc),
            )


async def _one_area_id(client):
    pool = await _pool(client)
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT id FROM areas")


async def test_stats_requires_a_session(app_client):
    for path in ("/stats", "/stats/areas"):
        assert (await app_client.get(path)).status_code == 401


async def test_city_totals_are_never_suppressed(authed_client):
    """A one-sighting corpus still returns real numbers: this names no place,
    so there is no small cell to protect."""
    client, _ = authed_client
    await _seed(client, sightings=[("solo", 12.9, 77.6, "2026-08-01")])
    body = (await client.get("/stats")).json()
    assert body["totals"]["sightings"] == 1


async def test_a_thin_area_is_omitted_and_counted(authed_client):
    client, _ = authed_client
    await _seed(
        client,
        areas=[("Thin", "bbmp_ward", "1", 77.0, 12.0, 1.0)],
        sightings=[("a", 12.5, 77.5, "2026-08-01")],
    )
    body = (await client.get("/stats/areas?kind=bbmp_ward")).json()
    assert body["areas"] == []
    assert body["areas_suppressed"] == 1


async def test_an_area_over_both_thresholds_is_reported(authed_client):
    client, _ = authed_client
    await _seed(
        client,
        areas=[("Domlur", "bbmp_ward", "112", 77.0, 12.0, 1.0)],
        sightings=[
            ("a", 12.5, 77.5, "2026-08-01"), ("a", 12.5, 77.5, "2026-08-02"),
            ("a", 12.5, 77.5, "2026-08-03"), ("b", 12.5, 77.5, "2026-08-04"),
            ("b", 12.5, 77.5, "2026-08-05"),
        ],
    )
    body = (await client.get("/stats/areas?kind=bbmp_ward")).json()
    assert len(body["areas"]) == 1
    area = body["areas"][0]
    assert area["name"] == "Domlur"
    assert area["ext_code"] == "112"
    assert area["sightings"] == 5
    assert area["observers"] == 2
    assert area["last_active_month"] == "2026-08"


async def test_unattributed_sightings_are_reported(authed_client):
    client, _ = authed_client
    await _seed(
        client,
        areas=[("Domlur", "bbmp_ward", "112", 77.0, 12.0, 1.0)],
        sightings=[("a", None, None, "2026-08-01"), ("a", 50.0, 10.0, "2026-08-02")],
    )
    body = (await client.get("/stats/areas?kind=bbmp_ward")).json()
    assert body["unattributed_sightings"] == 2


async def test_a_suppressed_area_by_id_is_a_404(authed_client):
    """Identical to a nonexistent id on purpose. A distinct 403 would turn
    the endpoint into an oracle for the fact suppression exists to withhold."""
    client, _ = authed_client
    await _seed(
        client,
        areas=[("Thin", "bbmp_ward", "1", 77.0, 12.0, 1.0)],
        sightings=[("a", 12.5, 77.5, "2026-08-01")],
    )
    area_id = await _one_area_id(client)
    assert (await client.get(f"/stats/areas/{area_id}")).status_code == 404
    assert (await client.get(f"/stats/areas/{uuid7()}")).status_code == 404


async def test_a_thin_month_is_dropped_from_an_areas_series(authed_client):
    """An area-month cell is a finer disclosure than the area row above it,
    so it is floored separately. Totals still include the dropped month."""
    client, _ = authed_client
    await _seed(
        client,
        areas=[("Domlur", "bbmp_ward", "112", 77.0, 12.0, 1.0)],
        sightings=[
            ("a", 12.5, 77.5, "2026-08-01"), ("a", 12.5, 77.5, "2026-08-02"),
            ("a", 12.5, 77.5, "2026-08-03"), ("b", 12.5, 77.5, "2026-08-04"),
            ("b", 12.5, 77.5, "2026-08-05"),
            ("b", 12.5, 77.5, "2026-09-01"),   # a lone September
        ],
    )
    area_id = await _one_area_id(client)
    body = (await client.get(f"/stats/areas/{area_id}")).json()
    assert [m["month"] for m in body["months"]] == ["2026-08"]
    assert body["area"]["sightings"] == 6


async def test_no_polygons_loaded_is_an_honest_empty_not_a_404(authed_client):
    client, _ = authed_client
    await _seed(client, sightings=[("a", 12.5, 77.5, "2026-08-01")])
    resp = await client.get("/stats/areas?kind=nothing_loaded")
    assert resp.status_code == 200
    body = resp.json()
    assert body["areas"] == []
    assert body["unattributed_sightings"] == 1


async def test_kind_defaults_to_config(authed_client):
    from app.config import settings

    client, _ = authed_client
    body = (await client.get("/stats/areas")).json()
    assert body["kind"] == settings.area_default_kind
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_stats.py -v`
Expected: FAIL — every request 404s, the router does not exist.

- [ ] **Step 3: Write the router**

Create `backend/app/routes/stats.py`:

```python
"""Counts over the corpus: how many people have seen how many dogs, and where.

The first IndieDex surface designed so that opening it to people who are not
signed in would be a dependency swap rather than a rewrite. Nothing here
returns a row, a coordinate, a photo or a name -- only counts, and only over
areas large enough that the count is not itself a location.

Cohort-gated today, deliberately. Taking the gate off is gated in turn on
rate limiting (now present), the privacy policy catching up (#53), and having
looked at real numbers against real suppression thresholds first.

Design: docs/specs/2026-09-13-aggregate-api-design.md. Tiers 2-4 of issue #58
-- individual profiles, credentialed precision -- are not built and would need
token auth, which this does not have.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from app import aggregates
from app.auth.deps import require_observer
from app.config import settings
from app.deps import get_conn

router = APIRouter()


@router.get("/stats")
async def get_stats(
    kind: str = Query(None, description="area scheme; defaults to area_default_kind"),
    _observer: UUID = Depends(require_observer),
    conn=Depends(get_conn),
):
    kind = kind or settings.area_default_kind
    rows = await aggregates.area_rows(conn, kind)
    kept, suppressed = aggregates.suppress(rows, kind)
    return {
        "kind": kind,
        "totals": await aggregates.city_totals(conn),
        "months": await aggregates.city_months(conn),
        "areas_reported": len(kept),
        "areas_suppressed": suppressed,
        "unattributed_sightings": await aggregates.unattributed_sightings(conn, kind),
    }


@router.get("/stats/areas")
async def get_stats_areas(
    kind: str = Query(None, description="area scheme; defaults to area_default_kind"),
    _observer: UUID = Depends(require_observer),
    conn=Depends(get_conn),
):
    kind = kind or settings.area_default_kind
    rows = await aggregates.area_rows(conn, kind)
    kept, suppressed = aggregates.suppress(rows, kind)
    return {
        "kind": kind,
        "areas": [{**r, "id": str(r["id"])} for r in kept],
        "areas_suppressed": suppressed,
        "unattributed_sightings": await aggregates.unattributed_sightings(conn, kind),
    }


@router.get("/stats/areas/{area_id}")
async def get_stats_area(
    area_id: UUID,
    _observer: UUID = Depends(require_observer),
    conn=Depends(get_conn),
):
    kind = await conn.fetchval("SELECT kind FROM areas WHERE id = $1", area_id)
    if kind is None:
        raise HTTPException(status_code=404)

    rows = await aggregates.area_rows(conn, kind)
    kept, _ = aggregates.suppress(rows, kind)
    area = next((r for r in kept if r["id"] == area_id), None)
    # 404, not 403, and identical to an id that was never issued. Saying
    # "this exists but is too thin to show you" discloses exactly the presence
    # fact suppression exists to withhold, one request at a time.
    if area is None:
        raise HTTPException(status_code=404)

    # The month series is floored separately: an area-month cell is a finer
    # disclosure than the area row that contains it, so an area can clear the
    # threshold on its lifetime numbers while a single month inside it holds
    # one sighting. The area's own totals still include the dropped months,
    # so the series deliberately does not sum to them.
    min_sightings, _min_observers = aggregates.thresholds(kind)
    months = [
        m for m in await aggregates.area_months(conn, area_id, kind)
        if m["sightings"] >= min_sightings
    ]
    return {"area": {**area, "id": str(area["id"])}, "months": months}
```

- [ ] **Step 4: Register the router**

Modify `backend/app/main.py` — add the import alongside the others:

```python
from app.routes.stats import router as stats_router
```

and the registration after `app.include_router(map_router)`:

```python
app.include_router(stats_router)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/test_stats.py -v`
Expected: 9 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/app/routes/stats.py backend/app/main.py backend/tests/test_stats.py
git commit -m "feat(stats): answer how many people have seen how many dogs"
```

---

### Task 5: Basic rate limiting

**Files:**
- Create: `backend/app/ratelimit.py`
- Modify: `backend/app/config.py`, `backend/app/routes/stats.py`, `backend/app/routes/join.py`, `backend/tests/conftest.py`, `deploy/Caddyfile`
- Test: `backend/tests/test_ratelimit.py`

**Interfaces:**
- Produces, importable from `app.ratelimit`:
  - `@dataclass(frozen=True) class Limit: times: int; window_s: int`
  - `def client_key(request: Request) -> str`
  - `def check(key: str, limit: Limit) -> None` — raises `HTTPException(429)` with `Retry-After`
  - `def reset() -> None` — clears all counters (tests)
  - `def stats_limit() -> Limit`, `def email_limit() -> Limit`, `def join_limit() -> Limit`
    — functions, not module constants, so a test can monkeypatch the underlying
    setting and have it take effect

- [ ] **Step 1: Add the config knobs**

Modify `backend/app/config.py` — append after the aggregates block from Task 3:

```python
    # --- rate limiting -----------------------------------------------------
    # In-process fixed windows. The container runs a single uvicorn with no
    # --workers, so the counts are exact. Two consequences, neither a bug but
    # both worth knowing: limits reset on deploy, and adding --workers would
    # silently multiply every limit below by the worker count.
    rate_limit_enabled: bool = True

    # Generous. Exists so a runaway client cannot spin the database, not to
    # ration anything.
    rl_stats_times: int = 60
    rl_stats_window_s: int = 60

    # Tight, and the reason rate limiting is in this change at all. This path
    # had no throttle of any kind and sits in front of production SES: an
    # unthrottled send endpoint is both a cost exposure and a way to mail-bomb
    # a third party. Keyed on IP *and* on the address, because IP alone lets a
    # rotating attacker hammer one inbox and lets one office NAT block itself.
    rl_email_times: int = 5
    rl_email_window_s: int = 900

    # A shared passcode with unlimited attempts is a shared passcode with no
    # passcode.
    rl_join_times: int = 10
    rl_join_window_s: int = 900

    # Read the client IP from X-Real-IP, which Caddy sets from the real peer
    # (deploy/Caddyfile). False for local dev and tests, where there is no
    # proxy and the header would be attacker-supplied.
    trust_proxy_header: bool = False
```

- [ ] **Step 2: Write the failing test**

Create `backend/tests/test_ratelimit.py`:

```python
"""Fixed-window counters, and the two ways they go wrong.

The first is memory: a map keyed by client IP with no bound is itself the
denial-of-service. The second is the key: if a client can choose it, the
limiter is decoration. Both have tests here.
"""

import pytest
from starlette.requests import Request

from app.config import settings
from app.ratelimit import Limit, check, client_key, reset


def _request(headers=None, client=("10.0.0.1", 1234)) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "headers": raw, "client": client, "method": "GET", "path": "/"})


@pytest.fixture(autouse=True)
def _clean():
    reset()
    yield
    reset()


def test_allows_up_to_the_limit_then_refuses():
    limit = Limit(times=3, window_s=60)
    for _ in range(3):
        check("k", limit)
    with pytest.raises(Exception) as exc:
        check("k", limit)
    assert exc.value.status_code == 429
    assert int(exc.value.headers["Retry-After"]) > 0


def test_keys_have_independent_budgets():
    limit = Limit(times=1, window_s=60)
    check("a", limit)
    check("b", limit)
    with pytest.raises(Exception):
        check("a", limit)


def test_the_store_stays_bounded_under_many_keys():
    """Otherwise the limiter is the attack: one request per forged key."""
    from app.ratelimit import _counter

    limit = Limit(times=10, window_s=60)
    for i in range(_counter._max_keys + 500):
        check(f"key-{i}", limit)
    assert len(_counter._hits) <= _counter._max_keys


def test_client_ip_ignores_headers_when_no_proxy_is_trusted(monkeypatch):
    """Locally and in tests there is no proxy, so a header is just something
    the caller typed. Trusting it would let one client be every client."""
    monkeypatch.setattr(settings, "trust_proxy_header", False)
    request = _request({"X-Real-IP": "1.2.3.4", "X-Forwarded-For": "1.2.3.4"})
    assert client_key(request) == "10.0.0.1"


def test_client_ip_uses_the_header_the_proxy_controls(monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy_header", True)
    assert client_key(_request({"X-Real-IP": "203.0.113.9"})) == "203.0.113.9"


def test_disabling_the_limiter_is_a_single_switch(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    limit = Limit(times=1, window_s=60)
    for _ in range(5):
        check("k", limit)


async def test_the_email_endpoint_stops_sending_after_the_limit(app_client, monkeypatch):
    """The endpoint this change exists for: it had no throttle at all."""
    monkeypatch.setattr(settings, "rl_email_times", 2)
    sent = []

    class _Sender:
        async def send(self, address, link):
            sent.append(address)

    monkeypatch.setattr("app.routes.join.get_sender", lambda: _Sender())

    for _ in range(2):
        resp = await app_client.post(
            "/auth/email", data={"email": "akash@dognosis.tech"},
            headers={"accept": "application/json"},
        )
        assert resp.status_code == 200
    resp = await app_client.post(
        "/auth/email", data={"email": "akash@dognosis.tech"},
        headers={"accept": "application/json"},
    )
    assert resp.status_code == 429
    assert len(sent) == 2


async def test_stats_is_limited_per_observer(authed_client, monkeypatch):
    monkeypatch.setattr(settings, "rl_stats_times", 2)
    client, _ = authed_client   # the fixture yields (client, observer_id)
    for _ in range(2):
        assert (await client.get("/stats")).status_code == 200
    assert (await client.get("/stats")).status_code == 429
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_ratelimit.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.ratelimit'`.

- [ ] **Step 4: Write the limiter**

Create `backend/app/ratelimit.py`:

```python
"""Fixed-window rate limiting, in process.

There was none of this anywhere in the application until now. The endpoint
that needed it most was never `/stats` -- it is the email login path, which
sits in front of production SES with no throttle of any kind.

In-process and no Redis: `deploy/entrypoint.sh` runs a single uvicorn with no
--workers, so one dict is the whole picture and the counts are exact. Two
consequences are deliberate rather than overlooked:

  - limits reset on deploy, which is fine for dampening abuse and would not be
    fine if this ever became a quota;
  - adding --workers would silently multiply every limit by the worker count,
    at which point this file is the thing to revisit.
"""

import time
from collections import OrderedDict
from dataclasses import dataclass

from fastapi import HTTPException
from starlette.requests import Request

from app.config import settings

# Sweep is O(n), so it runs every SWEEP_EVERY writes rather than on each one.
# The LRU cap is the real bound; the sweep just keeps idle memory honest.
SWEEP_EVERY = 1000


@dataclass(frozen=True)
class Limit:
    times: int
    window_s: int


class _FixedWindow:
    """Counters keyed by (key, window index), capped and LRU-evicted.

    The cap is not tidiness. A map keyed by client IP that grows without bound
    is itself the denial-of-service: one forged key per request and the
    limiter exhausts the box it was added to protect.
    """

    def __init__(self, max_keys: int = 10_000) -> None:
        self._hits: OrderedDict[tuple[str, int], tuple[int, float]] = OrderedDict()
        self._max_keys = max_keys
        self._writes = 0

    def clear(self) -> None:
        self._hits.clear()
        self._writes = 0

    def hit(self, key: str, limit: Limit, now: float) -> int | None:
        """Count one request. Returns None if allowed, else seconds to wait."""
        window = int(now // limit.window_s)
        expires_at = (window + 1) * limit.window_s
        slot = (key, window)

        count, _ = self._hits.get(slot, (0, expires_at))
        count += 1
        self._hits[slot] = (count, expires_at)
        self._hits.move_to_end(slot)

        self._writes += 1
        if self._writes % SWEEP_EVERY == 0:
            for stale in [s for s, (_, exp) in self._hits.items() if exp <= now]:
                del self._hits[stale]
        while len(self._hits) > self._max_keys:
            self._hits.popitem(last=False)

        if count > limit.times:
            return max(1, int(expires_at - now))
        return None


_counter = _FixedWindow()


def reset() -> None:
    _counter.clear()


def client_key(request: Request) -> str:
    """The caller's IP, from the one header the proxy controls.

    Caddy sets X-Real-IP from the actual peer and overwrites anything the
    client sent (deploy/Caddyfile). Reading X-Forwarded-For instead would mean
    depending on whether the proxy appends to or replaces a client-supplied
    value -- and getting that backwards is not a bug, it is a bypass: the
    limiter would key on a string the attacker chose.

    Off by default, because in dev and in tests there is no proxy in front and
    the header is simply something the caller typed.
    """
    if settings.trust_proxy_header:
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()
    return request.client.host if request.client else "unknown"


def check(key: str, limit: Limit) -> None:
    if not settings.rate_limit_enabled:
        return
    retry_after = _counter.hit(key, limit, time.time())
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Try again shortly.",
            headers={"Retry-After": str(retry_after)},
        )


# Keys are built by the caller rather than by a dependency factory. The
# authenticated surfaces key on `obs:<observer_id>` -- identity, not address,
# so they never consult a client-controllable header at all and two people
# behind one NAT do not share a budget. The unauthenticated ones key on
# `<scope>:ip:<addr>`, scoped so one endpoint's budget cannot be spent by
# another.


def stats_limit() -> Limit:
    return Limit(settings.rl_stats_times, settings.rl_stats_window_s)


def email_limit() -> Limit:
    return Limit(settings.rl_email_times, settings.rl_email_window_s)


def join_limit() -> Limit:
    return Limit(settings.rl_join_times, settings.rl_join_window_s)
```

- [ ] **Step 5: Apply the limiter to the three surfaces**

Modify `backend/app/routes/stats.py` — add the import and a router-level dependency, replacing `router = APIRouter()`:

```python
from app.ratelimit import check, stats_limit
```

and in each of the three handlers, immediately after the `kind` line (or first line of the body for the by-id handler):

```python
    check(f"obs:{_observer}", stats_limit())
```

Modify `backend/app/routes/join.py`:

```python
from app.ratelimit import check, client_key, email_limit, join_limit
```

In `join_submit`, as the first statement of the body:

```python
    check(f"join:ip:{client_key(request)}", join_limit())
```

In `email_submit`, immediately after `address = normalize_email(email)` and the
empty-address guard, before the allowlist check:

```python
    # Two keys, not one. IP alone lets a rotating attacker hammer a single
    # inbox, and lets one office NAT lock out everyone behind it; address
    # alone does nothing against a script walking a list. Both are fixed
    # windows -- this is dampening, not a lockout.
    check(f"email:ip:{client_key(request)}", email_limit())
    check(f"email:addr:{address}", email_limit())
```

- [ ] **Step 6: Reset counters between tests**

Modify `backend/tests/conftest.py` — add at the end:

```python
@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Every test starts with a full budget.

    The limiter stays *enabled* in the suite rather than switched off, so the
    dependencies are exercised on every request the tests make. No existing
    test makes more than two calls to any limited endpoint, so a per-test
    reset is enough to keep them green while keeping the limiter real.
    """
    from app.ratelimit import reset

    reset()
    yield
    reset()
```

- [ ] **Step 7: Give Caddy the header the limiter reads**

Modify `deploy/Caddyfile`:

```
{$APP_DOMAIN} {
	# X-Real-IP is set from the real peer and overwrites anything the client
	# sent. The app reads this rather than X-Forwarded-For so the limiter
	# never depends on whether a proxy appends to or replaces a
	# client-supplied value -- getting that backwards is not a bug, it is a
	# bypass, since the key becomes a string the caller chose.
	reverse_proxy app:8000 {
		header_up X-Real-IP {http.request.remote.host}
	}
}
```

Note that `trust_proxy_header` must be set to `true` in the deployed
environment for this to be read; it defaults to `false` so that dev and tests
never trust a header.

- [ ] **Step 8: Run the tests**

Run: `cd backend && uv run pytest tests/test_ratelimit.py -v`
Expected: 8 passed.

- [ ] **Step 9: Run the whole suite — the limiter touches shared paths**

Run: `cd backend && uv run pytest -q`
Expected: all pass. If a pre-existing test now 429s, it makes more calls than
the limit allows; raise that endpoint's limit via `monkeypatch` **in that
test**, never by weakening the default.

- [ ] **Step 10: Commit**

```bash
git add backend/app/ratelimit.py backend/tests/test_ratelimit.py backend/app/config.py backend/app/routes/stats.py backend/app/routes/join.py backend/tests/conftest.py deploy/Caddyfile
git commit -m "feat(ratelimit): put a ceiling on the endpoints that had none"
```

---

### Task 6: Migration — `individual_names`

**Files:**
- Create: `backend/migrations/versions/0009_individual_names.py`
- Test: `backend/tests/test_individual_names_schema.py`

**Interfaces:**
- Consumes: `individuals`, `observers` from 0001.
- Produces: the `individual_names` table. **No application code reads or writes it.**

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_individual_names_schema.py`:

```python
"""The names log, built before there are any names to lose.

Naming today is three columns on `individuals` -- a single, destructive,
last-writer-wins name, which is exactly the tourist-overwrites-resident
failure issue #4 exists to prevent. This table is the append-only half of the
fix. Nothing reads it yet; the migration is here because it is free while no
names exist and expensive afterwards.
"""

import pytest

from app.ids import uuid7


async def _individual(conn):
    iid = uuid7()
    await conn.execute("INSERT INTO individuals (id) VALUES ($1)", iid)
    return iid


async def test_a_name_can_be_proposed_by_nobody(migrated_db):
    """Model suggestions and WhatsApp intake have no account behind them.
    NOT NULL here would force a fake observer row per name, which is worse
    than an honest null."""
    iid = await _individual(migrated_db)
    await migrated_db.execute(
        "INSERT INTO individual_names (id, individual_id, name) VALUES ($1, $2, 'Kaju')",
        uuid7(), iid,
    )
    row = await migrated_db.fetchrow("SELECT status, proposed_by FROM individual_names")
    assert row["status"] == "proposed"
    assert row["proposed_by"] is None


async def test_many_names_may_be_proposed_for_one_dog(migrated_db):
    """Kaju to the woman at the gate, Blackie to the shop two doors down.
    Nothing here decides between them -- that is issue #55."""
    iid = await _individual(migrated_db)
    for name in ("Kaju", "Blackie", "Kalu"):
        await migrated_db.execute(
            "INSERT INTO individual_names (id, individual_id, name) VALUES ($1, $2, $3)",
            uuid7(), iid, name,
        )
    assert await migrated_db.fetchval("SELECT count(*) FROM individual_names") == 3


async def test_only_one_name_can_be_active_at_a_time(migrated_db):
    """`individuals.name` caches the active row. The database refuses the
    inconsistent state rather than trusting code that does not exist yet."""
    iid = await _individual(migrated_db)
    await migrated_db.execute(
        "INSERT INTO individual_names (id, individual_id, name, status) "
        "VALUES ($1, $2, 'Kaju', 'active')",
        uuid7(), iid,
    )
    with pytest.raises(Exception):
        await migrated_db.execute(
            "INSERT INTO individual_names (id, individual_id, name, status) "
            "VALUES ($1, $2, 'Bruno', 'active')",
            uuid7(), iid,
        )


async def test_two_dogs_may_each_have_an_active_name(migrated_db):
    for _ in range(2):
        iid = await _individual(migrated_db)
        await migrated_db.execute(
            "INSERT INTO individual_names (id, individual_id, name, status) "
            "VALUES ($1, $2, 'Kaju', 'active')",
            uuid7(), iid,
        )
    assert await migrated_db.fetchval(
        "SELECT count(*) FROM individual_names WHERE status = 'active'"
    ) == 2


async def test_status_is_constrained(migrated_db):
    iid = await _individual(migrated_db)
    with pytest.raises(Exception):
        await migrated_db.execute(
            "INSERT INTO individual_names (id, individual_id, name, status) "
            "VALUES ($1, $2, 'Kaju', 'canonical')",
            uuid7(), iid,
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_individual_names_schema.py -v`
Expected: FAIL — relation `individual_names` does not exist.

- [ ] **Step 3: Write the migration**

Create `backend/migrations/versions/0009_individual_names.py`:

```python
"""individual_names: one row per naming event, nothing ever overwritten

Identity has had an event log since 0001 -- `match_proposals` and
`confirmations` record every claim that two sightings are one dog, and no
verdict is destroyed. Naming has had three columns on `individuals`: `name`,
`named_by`, `named_at`. That is a single, destructive, last-writer-wins name,
which is precisely the failure issue #4 is written to prevent: a dog already
called Kaju by the woman who has fed him for three years renamed Bruno by
someone who walked past once, with no history and no way back.

This table is the append-only half of the fix. A name is an event: proposed by
someone, possibly resolved by someone else, never deleted. A bad rename
becomes a one-click revert rather than a loss.

**Nothing reads or writes this table yet.** It is here now because the
migration is free while no names exist and expensive once they do -- #4 and
#55 both identify it as the one naming decision with a closing window. Who may
name a dog (#4), what a name even is when a dog has three (#55), and what
earns the standing to decide (#56) are all deliberately unbuilt.

`individuals.name` / `named_by` / `named_at` stay where they are and become a
*cache* of the active row -- the same caches-over-an-event-log pattern as
`sightings.match_status` over `confirmations`. Reads stay cheap, the log stays
authoritative. No code changes, because no code writes names today.

Two choices worth stating:

`proposed_by` is nullable, answering #4's open question. A name can arrive
from a model suggestion or a WhatsApp intake with no account behind it, and
NOT NULL would force a fake observer row for every such name.

The partial unique index is the real invariant: at most one `active` name per
individual, which is exactly what the cache column can represent. The database
refuses the inconsistent state rather than trusting application code that has
not been written yet to avoid it.

Revision ID: 0009_individual_names
Revises: 0008_areas_kind
Create Date: 2026-09-13

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0009_individual_names"
down_revision: Union[str, None] = "0008_areas_kind"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE individual_names (
            id uuid PRIMARY KEY,
            individual_id uuid NOT NULL REFERENCES individuals(id),
            name text NOT NULL,
            proposed_by uuid REFERENCES observers(id),
            status text NOT NULL DEFAULT 'proposed'
                CHECK (status IN ('proposed','active','superseded','rejected')),
            created_at timestamptz NOT NULL DEFAULT now(),
            resolved_by uuid REFERENCES observers(id),
            resolved_at timestamptz
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_individual_names_individual_id "
        "ON individual_names (individual_id);"
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_individual_names_active "
        "ON individual_names (individual_id) WHERE status = 'active';"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS individual_names;")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/test_individual_names_schema.py -v`
Expected: 5 passed.

- [ ] **Step 5: Add `individual_names` to the test truncation list**

Same reason as `areas` in Task 1: a row left behind by one test changes the
next one's result. Extend the list in **both** places in
`backend/tests/conftest.py`:

```python
            "TRUNCATE observers, sightings, photos, embeddings, individuals, "
            "match_proposals, confirmations, clinical_records, login_tokens, "
            "areas, individual_names RESTART IDENTITY CASCADE"
```

- [ ] **Step 6: Run the file twice to confirm isolation**

Run: `cd backend && uv run pytest tests/test_individual_names_schema.py -v` twice.
Expected: 5 passed both times.

- [ ] **Step 7: Commit**

```bash
git add backend/migrations/versions/0009_individual_names.py backend/tests/test_individual_names_schema.py backend/tests/conftest.py
git commit -m "feat(names): give naming an event log before there are names to lose"
```

---

### Task 7: Documentation and the issue that stays open

**Files:**
- Modify: `AGENTS.md`, `docs/specs/2026-09-13-aggregate-api-design.md`

- [ ] **Step 1: Reconcile the spec with what was built**

The spec proposes `area_suppression_fallback: tuple[int, int] = (5, 2)`; the
implementation uses two scalars (`area_min_sightings_default`,
`area_min_observers_default`) to match the flat-scalar style of the rest of
`config.py`. Update that line in
`docs/specs/2026-09-13-aggregate-api-design.md` so the doc and the code agree.

- [ ] **Step 2: Document the endpoints in AGENTS.md**

Append this section to `AGENTS.md`, after the existing endpoint documentation:

```markdown
## Counts: `/stats`

Three read-only endpoints returning derived counts and nothing else — no row,
coordinate, photo or name leaves them. That is what makes this the one tier of
issue #58 that could eventually be read by someone who is not signed in.

    GET /stats?kind=…              city-wide totals + monthly series
    GET /stats/areas?kind=…        per-area rows
    GET /stats/areas/{id}          one area, with its own monthly series

**Cohort-gated today.** Opening them is a dependency swap, gated on three
things: rate limiting (present), the privacy policy catching up (#53), and
having looked at real numbers against real suppression thresholds first.

**`kind` is the boundary scheme**, defaulting to `settings.area_default_kind`.
Wards, PIN codes, neighbourhood outlines and hand-drawn pilot polygons are all
rows in `areas` differing only by `kind`, loaded by `scripts/load_areas.py`.
Changing which scheme the public number carries is a config line and a loader
run, not a code change. An unknown kind is a 200 with an empty `areas` list,
not a 404 — "no polygons loaded" is a legitimate state and it reads honestly:
every sighting is unattributed because nothing exists to attribute it to.

**Three numbers, never one called "dogs".** `sightings` overcounts dogs (one
dog seen ten times is ten); `confirmed_individuals` undercounts them (every
unmatched sighting is invisible to it, and `match_status` defaults to
`unmatched`). The truth is between them and the API does not guess.

**Suppression.** An area is reported only if it clears both
`area_min_sightings` and `area_min_observers` for its kind — per kind, because
the threshold protects a privacy property that depends on cell size. A
suppressed area is **omitted entirely**, never returned with a flag: a flag
still discloses "at least one dog is here". Fetching one by id returns 404,
identical to an id that never existed, so the endpoint cannot be used as an
oracle. An area's monthly series is floored separately, because an area-month
cell is a finer disclosure than the area row containing it.

**The numbers reconcile.** Per-area rows will not sum to the city total —
areas drop out under suppression, sightings can have no location at all
(`geo_source` allows `'none'`), and a sighting can fall outside every polygon
of a kind. Hence `areas_suppressed` and `unattributed_sightings` on every
response. Without them the difference looks like missing dogs.

**Months only**, bucketed in `Asia/Kolkata`. There is no finer grain and no
API to ask for one — that is issue #5's "delay" dial enforced by absence
rather than by a rule someone has to remember.

`app/aggregates.py` owns the single definition of a countable sighting
(`review_status <> 'rejected'`) and the lateral join that attributes a sighting
to at most one area. `/map` and `/dogs` import that constant. `/dex` still does
not filter rejected sightings — that is issue #54's call about what your own
Journal shows, deliberately not changed here.

## Rate limiting

`app/ratelimit.py` — in-process fixed windows, applied to `/stats`,
`/auth/email` and `/auth/join`. The email path is why this exists: it had no
throttle of any kind and sits in front of production SES.

Authenticated surfaces key on `observer_id`; unauthenticated ones key on the
client IP read from `X-Real-IP`, which Caddy sets from the real peer and
overwrites (`deploy/Caddyfile`). `trust_proxy_header` gates that and defaults
to **false** — a limiter keyed on a value the caller chose is worse than none,
because it looks like protection.

Single uvicorn, no `--workers`, so the counts are exact. Limits reset on
deploy, and adding workers would silently multiply every limit by the worker
count. Both are fine for dampening abuse and neither would be fine for a quota.
```

- [ ] **Step 3: Run the full suite one last time**

Run: `cd backend && uv run pytest -q`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add AGENTS.md docs/specs/2026-09-13-aggregate-api-design.md
git commit -m "docs: describe the stats endpoints and their suppression contract"
```

- [ ] **Step 5: File the follow-up issue**

```bash
gh issue create --title "Auth hardening beyond basic rate limiting" --body "$(cat <<'EOF'
Split out of the aggregate API work (see `docs/specs/2026-09-13-aggregate-api-design.md`).

That change added fixed-window rate limiting to `/auth/email`, `/auth/join` and
`/stats` — dampening, deliberately nothing more. The following were considered
and left out as a separate piece of work:

- Progressive backoff or lockout after repeated failures.
- Per-address cooldowns distinct from the shared window.
- Captcha or proof-of-work on the passcode door.
- Persisting limiter state, so limits survive a deploy. In-process counters
  reset on every restart, which is fine for abuse dampening and not fine if
  this ever becomes a quota.
- Revisiting all of the above if `uvicorn --workers` is ever added: every
  limit silently multiplies by the worker count.
EOF
)"
```

---

## Verification before the PR

- [ ] `cd backend && uv run pytest -q` — the whole suite passes.
- [ ] `cd backend && ALEMBIC_DB_URL=... uv run alembic downgrade 0007_geo_source_exif && ... upgrade head` — both migrations reverse and re-apply.
- [ ] **Verify the client IP empirically against the deployed box** before trusting the limiter: `curl -s -H "X-Real-IP: 1.2.3.4" -H "X-Forwarded-For: 5.6.7.8" https://app.nammaindies.org/health` with the resolved key logged, confirming Caddy's `header_up` overwrote the client-supplied `X-Real-IP`. Until this is done, `trust_proxy_header` stays `false` in the deployed environment — a limiter keyed on a value the caller chose is worse than none, because it looks like protection.
- [ ] Confirm `areas` is genuinely empty on prod before the deploy runs 0008.
