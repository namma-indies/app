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
