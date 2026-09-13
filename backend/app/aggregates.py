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
