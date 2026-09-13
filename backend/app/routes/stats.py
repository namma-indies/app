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
from app.auth.deps import require_moderator, require_observer
from app.config import settings
from app.deps import get_conn
from app.ratelimit import check, stats_limit

router = APIRouter()


@router.get("/stats")
async def get_stats(
    kind: str = Query(None, description="area scheme; defaults to area_default_kind"),
    _observer: UUID = Depends(require_observer),
    conn=Depends(get_conn),
):
    check(f"obs:{_observer}", stats_limit())
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
    check(f"obs:{_observer}", stats_limit())
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
    check(f"obs:{_observer}", stats_limit())
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


@router.get("/stats/observers")
async def get_stats_observers(
    _moderator: UUID = Depends(require_moderator),
    conn=Depends(get_conn),
):
    """Who is contributing, and how much. Moderator-gated, unlike /stats.

    The rest of this router returns counts over areas and is built to be
    readable by a stranger one day. This is not that: these are named people
    with contact addresses, and it exists so an operator can see their own
    pilot cohort. The public, opt-in version of "how many dogs has this person
    seen" is a separate and deliberately unbuilt question (#58, #5).

    404 rather than 403 for a non-moderator -- see require_moderator.
    """
    check(f"obs:{_moderator}", stats_limit())
    rows = await aggregates.observer_rows(conn)
    return {
        "observers": [
            {**r, "id": str(r["id"])}
            for r in rows
        ]
    }
