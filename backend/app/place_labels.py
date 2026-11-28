"""Where a sighting was, in words.

"12.975, 77.639" is what the app used to say. A field note says "Indiranagar,
Bengaluru". This turns map cells into neighbourhood names.

THE NAME IS THE CELL'S, NOT THE POINT'S
---------------------------------------
Every name is looked up for the centre of the point's cell on the same grid
`/map` and `/dogs` coarsen other people's sightings to (`precision.coarsen`,
`map_coarsen_cell_m`). So a name never says more than the map already shows
anyone about anyone: for someone else's sighting it is the cell they were
already shown, put into words. One name per cell also means one lookup per
cell, ever, which is what keeps this inside Nominatim's terms. It also means
the name of a cell straddling two neighbourhoods is whichever one its centre
falls in, so surfaces say "around" for anyone else's sighting.

NO REQUEST WAITS ON THE GEOCODER
--------------------------------
`labels_for` answers from the table and adds a row for every cell it has not
seen. The worker (`consumer`, started with the app) fills those rows in at one
request a second, sharing the place search's throttle so the two together stay
inside the provider's limit. A surface shows coordinates until the name
arrives, which for a new cell is the next time it is opened.

The provider sees this server's address and cell centres. It never sees a
person, a sighting, or an exact point.
"""
import asyncio
import logging
import time
from collections.abc import Iterable

import asyncpg
import httpx

from app.config import settings
from app.precision import coarsen

logger = logging.getLogger(__name__)

# How long a lookup that found nothing, or failed, waits before another try.
RETRY_AFTER_S = 24 * 3600
# The suburb first: it is about the size of a cell, and the name people use
# ("Indiranagar"), where OSM's neighbourhood is finer and often obscure ("Old
# Binnamangala") and would claim more precision than the cell has. A city
# alone is the fallback for a cell out in the country.
_LOCAL = ("suburb", "neighbourhood", "quarter", "residential", "city_district",
          "village", "hamlet", "town")
_WIDER = ("city", "town", "village", "municipality", "county", "state_district")


def cell_of(lat: float, lng: float) -> tuple[int, float, float]:
    cell_m = int(settings.map_coarsen_cell_m)
    clat, clng = coarsen(lat, lng, cell_m)
    return cell_m, clat, clng


def label_from(reply: dict) -> str | None:
    """`Indiranagar, Bengaluru`, from a Nominatim reverse-geocoding reply."""
    address = reply.get("address") if isinstance(reply, dict) else None
    if not isinstance(address, dict):
        return None
    local_key = next((k for k in _LOCAL if address.get(k)), None)
    local = str(address[local_key]).strip() if local_key else None
    # From a different field than the local name: a village is both, and
    # "Kakati, Kakati" says nothing the first half did not.
    wider = next((str(address[k]).strip() for k in _WIDER if k != local_key and address.get(k)), None)
    parts = [p for p in (local, wider) if p]
    if len(parts) == 2 and parts[0].casefold() == parts[1].casefold():
        parts = parts[:1]
    label = ", ".join(parts)[:120].strip()
    return label or None


async def _reverse(lat: float, lng: float) -> str | None:
    """Ask the geocoder for a cell centre's name. Raises on a failed request."""
    params = {"lat": f"{lat:.5f}", "lon": f"{lng:.5f}", "format": "jsonv2",
              "zoom": "16", "addressdetails": "1", "accept-language": "en"}
    async with httpx.AsyncClient(trust_env=False, timeout=settings.geocode_timeout_s) as client:
        response = await client.get(settings.geocode_reverse_url, params=params,
                                    headers={"User-Agent": settings.geocode_user_agent})
    response.raise_for_status()
    return label_from(response.json())


async def labels_for(conn: asyncpg.Connection, points: Iterable[tuple[float, float]]) -> dict[tuple[float, float], str]:
    """Names for whichever of these points' cells have one, keyed by the
    point as given. Cells never asked about before are queued for the worker."""
    by_cell: dict[tuple[int, float, float], list[tuple[float, float]]] = {}
    for lat, lng in points:
        if lat is None or lng is None:
            continue
        by_cell.setdefault(cell_of(lat, lng), []).append((lat, lng))
    if not by_cell:
        return {}
    cells = list(by_cell)
    rows = await conn.fetch(
        "SELECT cell_m, lat, lng, label FROM place_labels "
        "WHERE (cell_m, lat, lng) IN (SELECT * FROM unnest($1::int[], $2::float8[], $3::float8[]))",
        [c[0] for c in cells], [c[1] for c in cells], [c[2] for c in cells])
    known = {(r["cell_m"], r["lat"], r["lng"]): r["label"] for r in rows}
    missing = [c for c in cells if c not in known]
    if missing and settings.place_labels_enabled:
        await conn.execute(
            "INSERT INTO place_labels (cell_m, lat, lng) "
            "SELECT * FROM unnest($1::int[], $2::float8[], $3::float8[]) ON CONFLICT DO NOTHING",
            [c[0] for c in missing], [c[1] for c in missing], [c[2] for c in missing])
    return {point: label for cell, label in known.items() if label for point in by_cell[cell]}


async def fill_one(conn: asyncpg.Connection) -> bool:
    """Name the oldest unnamed cell that is due. False when none is due."""
    from app.routes import places  # the shared one-a-second throttle

    row = await conn.fetchrow(
        "SELECT cell_m, lat, lng FROM place_labels WHERE label IS NULL "
        "AND (looked_up_at IS NULL OR looked_up_at < now() - make_interval(secs => $1)) "
        "ORDER BY looked_up_at NULLS FIRST, created_at LIMIT 1",
        float(RETRY_AFTER_S))
    if row is None:
        return False
    async with places._lock:
        wait = places._last_request + 1.0 - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            label = await _reverse(row["lat"], row["lng"])
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("place label lookup failed (%s); will retry later", type(exc).__name__)
            label = None
        finally:
            places._last_request = time.monotonic()
    await conn.execute(
        "UPDATE place_labels SET label=$4, looked_up_at=now() WHERE cell_m=$1 AND lat=$2 AND lng=$3",
        row["cell_m"], row["lat"], row["lng"], label)
    return True


async def consumer(pool: asyncpg.Pool) -> None:
    """Name new cells as they turn up, one a second at most."""
    while True:
        try:
            async with pool.acquire() as conn:
                worked = await fill_one(conn)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("place label worker failed (%s); will retry", type(exc).__name__)
            worked = False
        if not worked:
            await asyncio.sleep(settings.place_labels_idle_s)
