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


def coarse_geog(column: str = "s.geog") -> str:
    """SQL for the grid-cell centre a sighting is shown at to anyone who did not
    log it (`app.precision`; the SQL side is migration 0022).

    Anything a query decides about someone else's sighting -- which area it is
    counted in, whether it is near enough to be a match candidate -- is decided
    on this rather than on the exact point, so the answer depends on the cell
    alone. Decided on the exact point, the same answer near an edge says which
    side of it the sighting is, which the cell does not.
    """
    return f"coarse_point({column}, {float(settings.map_coarsen_cell_m)!r}::float8)"


def animal_present() -> str:
    """Is there an animal in this sighting, as far as anyone can tell?

    `animal_override` is a moderator's ruling and wins outright; NULL means
    nobody has looked and the detector's number decides. A NULL score is
    "never scored" and reads as present -- fail open, so a detector failure
    costs a label and never someone's photograph.

    A function rather than a constant because the threshold has to be varied
    per test, and an f-string evaluated at import cannot be.
    """
    lo = settings.animal_confidence_min
    # `animal_confidence` is `real`; an unsuffixed decimal literal is `numeric`,
    # and `real >= numeric` promotes the `real` to `numeric` rather than
    # rounding the literal to `real` -- so e.g. 0.29::real >= 0.29 is FALSE.
    # The `::real` cast makes both sides compare as `real` and the boundary
    # inclusive, as `>=` promises. (`{lo:g}` also caps at 6 significant
    # digits, but no plausible threshold needs a 7th -- left as `:g` for
    # readable SQL.)
    return (
        "COALESCE(s.animal_override, "
        f"s.animal_confidence IS NULL OR s.animal_confidence >= {lo:g}::real)"
    )


def countable_sighting() -> str:
    """The one definition. `/map` and `/dogs` call it rather than restating it.

    `= 'valid'`, not `<> 'rejected'`. Since #46 gave `review_status` a writer,
    `pending` means someone reported this and no human has looked yet -- the
    whole point of that state is that it waits somewhere other than a public
    surface.

    `duplicate_of IS NULL`: a photo submitted twice is one observation, so the
    later copy does not count again (app.identical, migration 0016). It still
    exists, and still shows in its contributor's own /dex.
    """
    return (f"s.review_status = 'valid' AND s.duplicate_of IS NULL AND {animal_present()}"
            f" AND {owner_active('s')} AND {screened('s')}")


def owner_active(alias: str = "s") -> str:
    """The sighting's observer still stands behind it.

    A banned observer's sightings are off every shared surface (#180).
    Someone who has asked to delete their account (#179) is off every shared
    surface from that moment, not when the purge gets to them: the grace
    period hides, and the purge erases. A deleted account has nothing left to
    show, and a passcode account absorbed into an email one (deleted_at, with
    its sightings moved) has nothing either. Decided by who logged it, never
    by where, so it cannot reveal anything about a location (#148, #150).
    """
    return (f"NOT EXISTS (SELECT 1 FROM observers ox WHERE ox.id = {alias}.observer_id"
            f" AND (ox.deletion_requested_at IS NOT NULL OR ox.deleted_at IS NOT NULL"
            f" OR ox.banned_at IS NOT NULL))")


def screened(alias: str = "s") -> str:
    """The upload this sighting came from has passed screening (#181).

    Unscreened, held or rejected uploads are off every shared surface: only
    their owner (`/dex`) and moderators see them. Written so that anything
    not positively cleared stays hidden: a capture that is missing, or in a
    state added later, does not show. Next to `owner_active` wherever a
    surface shows someone else's sightings.
    """
    wait = float(settings.screening_model_wait_s)
    # `checking`: the detector cleared it and the local model is looking. It
    # shows once the model has had its time, so a model that is down never
    # blanks the map (app/model_screening.py).
    return (f"EXISTS (SELECT 1 FROM captures cs WHERE cs.id = {alias}.capture_id"
            f" AND (cs.screening IN ('clear','approved')"
            f" OR (cs.screening = 'checking'"
            f" AND cs.screened_at <= now() - {wait!r}::float8 * interval '1 second')))")


def visible_to(viewer: str, alias: str = "s") -> str:
    """Neither the viewer nor the sighting's observer has blocked the other (#180).

    Both directions, so that blocking someone also stops them seeing where you
    go. `viewer` is the SQL for the viewer's id, usually a parameter. Like
    `owner_active`, it is decided by who logged a sighting and never by where,
    so a filtered answer is always a subset of what the cell rules already
    allowed (#148, #150). A moderator ban is in `owner_active`: it hides from
    everyone, this only from one person.
    """
    return (f"NOT EXISTS (SELECT 1 FROM observer_blocks bx WHERE"
            f" (bx.blocker_id = {viewer} AND bx.blocked_id = {alias}.observer_id)"
            f" OR (bx.blocked_id = {viewer} AND bx.blocker_id = {alias}.observer_id))")


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
#
# Attributed by the sighting's grid cell centre, not its exact point. On the
# exact point, a boundary through a cell would split it, and which side a
# sighting was counted on would place it more finely than the cell it is shown
# as. `app.social.area_name` uses the same rule for the same reason.
def _area_lateral() -> str:
    return f"""
    LEFT JOIN LATERAL (
        SELECT a.id, a.name, a.ext_code
        FROM areas a
        WHERE a.kind = $1 AND ST_Covers(a.geog, {coarse_geog()})
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
        WHERE {countable_sighting()}
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
        WHERE {countable_sighting()}
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
        {_area_lateral()}
        WHERE {countable_sighting()} AND a.id IS NOT NULL
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
        {_area_lateral()}
        WHERE {countable_sighting()} AND a.id IS NULL
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
        {_area_lateral()}
        WHERE {countable_sighting()} AND a.id = $2
        GROUP BY 1
        ORDER BY 1
        """,
        kind, area_id,
    )
    return [dict(r) for r in rows]


async def observer_rows(conn) -> list[dict]:
    """Every observer, with what they have contributed. Moderator-only.

    Not an aggregate in the sense the rest of this module means it: these are
    named people, not counts over an area, and nothing here may be exposed on
    a public surface. The opt-in public profile question (#58, #5) is
    deliberately unbuilt -- this is the operator's view of their own pilot
    cohort, not a directory.

    Observers with no sightings are included. Someone who signed in and never
    logged anything is exactly the thing an operator wants to see.
    """
    rows = await conn.fetch(
        f"""
        SELECT
            o.id,
            o.display_name,
            o.email,
            -- How many people have blocked them, and whether a moderator has
            -- banned them (#180): a signal for a person to read, never a trigger.
            (SELECT count(*) FROM observer_blocks b WHERE b.blocked_id = o.id) AS blocked_by,
            o.banned_at IS NOT NULL AS banned,
            o.created_via,
            o.trust_tier,
            o.created_at,
            COUNT(s.id) FILTER (WHERE {countable_sighting()}) AS sightings,
            COUNT(DISTINCT s.individual_id) FILTER (WHERE {countable_sighting()})
                AS confirmed_individuals,
            MAX(s.captured_at) FILTER (WHERE {countable_sighting()}) AS last_sighting_at
        FROM observers o
        LEFT JOIN sightings s ON s.observer_id = o.id
        WHERE o.deleted_at IS NULL AND o.deletion_requested_at IS NULL
        GROUP BY o.id, o.display_name, o.email, o.created_via, o.trust_tier, o.created_at, o.banned_at
        ORDER BY sightings DESC, o.created_at ASC
        """
    )
    return [dict(r) for r in rows]
