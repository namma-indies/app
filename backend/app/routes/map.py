"""The cohort-wide map: every signed-in observer's sightings, as pins.

Deliberately not a scope flag on `/dex`. `/dex` means "my dex" -- its
`WHERE observer_id = $1` is the ownership semantics that `resolve_sighting` and
the standing model in issue #5 both read, and overloading it would blur that.
The payload differs too: the grid wants full-resolution originals, a map wants
thumbnails and nothing else.

**Visibility.** The whole authenticated cohort, at full precision. That is safe
today only because the cohort is passcode- and allowlist-gated -- these are
vetted testers, not the public. A *public* map needs issue #5's
`resolve_precision` coarsening (resolve to an area label, never a fuzzed point)
before it can show a stranger where a named dog is. That is not built here, and
this endpoint is where it would go.

One request serves both sides of the Mine/Everyone toggle: each sighting
carries `mine`, so flipping the toggle filters what the client already has.
At pilot scale that is strictly better than two endpoints; if the corpus
outgrows one response, `bbox` is already the seam to page on.
"""

import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.deps import require_observer
from app.deps import get_conn, get_storage
from app.photos import thumb_key
from app.storage.s3 import S3Storage

router = APIRouter()

# High enough to be irrelevant at pilot scale, low enough that a runaway client
# cannot ask the box to presign a URL for every photo it has ever stored.
MAX_LIMIT = 5000
DEFAULT_LIMIT = 2000


def _parse_bbox(bbox: str | None) -> tuple[float, float, float, float] | None:
    """"west,south,east,north" in degrees, or None for no filter.

    Rejected rather than ignored when malformed: silently dropping an
    unparseable viewport turns a bounded query into a full scan, which is the
    kind of thing that looks fine until the corpus is large.
    """
    if bbox is None:
        return None
    parts = bbox.split(",")
    if len(parts) != 4:
        raise HTTPException(status_code=422, detail="bbox must be west,south,east,north")
    try:
        west, south, east, north = (float(p) for p in parts)
    except ValueError:
        raise HTTPException(status_code=422, detail="bbox values must be numbers")
    if not (west < east and south < north):
        raise HTTPException(status_code=422, detail="bbox must have west<east and south<north")
    if not (-180 <= west <= 180 and -180 <= east <= 180):
        raise HTTPException(status_code=422, detail="bbox longitudes out of range")
    if not (-90 <= south <= 90 and -90 <= north <= 90):
        raise HTTPException(status_code=422, detail="bbox latitudes out of range")
    return west, south, east, north


@router.get("/map")
async def get_map(
    bbox: str | None = Query(None, description="west,south,east,north in degrees"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    observer_id: UUID = Depends(require_observer),
    conn=Depends(get_conn),
    storage: S3Storage = Depends(get_storage),
):
    envelope = _parse_bbox(bbox)

    # DISTINCT ON collapses a multi-photo sighting to one row here rather than
    # in Python. A clip yields up to twelve frames under one sighting and the
    # map needs exactly one thumbnail, so joining every photo would multiply
    # every pin and then throw the extras away.
    sql = """
        SELECT DISTINCT ON (s.id)
            s.id,
            s.captured_at,
            ST_Y(s.geog::geometry) AS lat,
            ST_X(s.geog::geometry) AS lng,
            s.geo_accuracy_m,
            s.attrs,
            s.observer_id,
            o.display_name AS observer,
            p.s3_key
        FROM sightings s
        JOIN observers o ON o.id = s.observer_id
        LEFT JOIN photos p ON p.sighting_id = s.id
        WHERE s.geog IS NOT NULL
          -- `= 'valid'`, not `<> 'rejected'`. `pending` means someone reported
          -- this and no human has looked yet; the whole point of that state is
          -- that it is off the map while it waits.
          AND s.review_status = 'valid'
    """
    args: list = []
    if envelope is not None:
        west, south, east, north = envelope
        sql += (
            " AND ST_Intersects(s.geog,"
            " ST_MakeEnvelope($1, $2, $3, $4, 4326)::geography)"
        )
        args += [west, south, east, north]

    # The DISTINCT ON key leads the sort, as Postgres requires; the ordering
    # that actually matters (newest first) is applied to the collapsed set
    # below, since DISTINCT ON dictates this one.
    #
    # p.id breaks the tie, and it is not decorative. Every photo of a sighting
    # is inserted in one transaction and Postgres holds now() constant across
    # it, so they all carry an identical created_at -- measured at 22 of 22
    # multi-photo sightings. With created_at alone the winner is left to the
    # plan, so the pin for a video sighting can show a different frame on each
    # load. p.id is uuid7, so it tiebreaks by real insertion order.
    sql += f"""
        ORDER BY s.id, p.created_at ASC, p.id ASC
        LIMIT ${len(args) + 1}
    """
    args.append(limit)

    rows = await conn.fetch(sql, *args)

    # Presigned in one batch rather than per row. Signing is a local HMAC, but
    # opening an aioboto3 client per call is not: 2.86 ms each that way against
    # 0.20 ms sharing one. At this endpoint's default limit that is the
    # difference between ~5.7 s and ~0.4 s of CPU per request.
    #
    # Thumbnails only. A full-resolution WebP per pin is the difference between
    # a map that loads on Indian mobile data and one that does not -- and the
    # original is one tap away via /dex anyway.
    keyed = [(i, thumb_key(r["s3_key"])) for i, r in enumerate(rows) if r["s3_key"] is not None]
    thumb_urls = await storage.urls([key for _, key in keyed])
    thumb_by_row = {i: url for (i, _), url in zip(keyed, thumb_urls)}

    sightings = []
    for i, row in enumerate(rows):
        raw_attrs = row["attrs"]
        attrs = json.loads(raw_attrs) if isinstance(raw_attrs, str) else (raw_attrs or {})
        thumb_url = thumb_by_row.get(i)
        sightings.append(
            {
                "id": str(row["id"]),
                "captured_at": row["captured_at"],
                "lat": row["lat"],
                "lng": row["lng"],
                "geo_accuracy_m": row["geo_accuracy_m"],
                "attrs": attrs,
                "observer": row["observer"],
                "mine": row["observer_id"] == observer_id,
                "photos": [{"thumb_url": thumb_url}] if thumb_url else [],
            }
        )

    sightings.sort(key=lambda s: s["captured_at"], reverse=True)
    return {"sightings": sightings}
