"""Place search for the pick-on-a-map screen (#80).

When the phone can't say where it is, the fallback is a map that opens over a
whole city. Panning and pinching from there down to one street is hard on a
phone and gets the pin wrong, and a wrong pin is wrong data: `geog` feeds the
candidate search that decides which dogs a sighting is compared with. Typing
"Domlur 5th cross" and landing there is the fix.

WHY A PROXY, NOT A CALL FROM THE APP
------------------------------------
The geocoder is OpenStreetMap's Nominatim, whose public service is free on
terms: an identifying User-Agent, at most one request per second, results
cached, and no search-as-you-type. Going through this route is how those
terms are met for every client at once:

- one place sends requests, so the 1/s ceiling holds across all testers;
- answers are cached, so the same street looked up twice is one request;
- testers' IP addresses, and what they search for from where, never reach a
  third party; only this server's address does.

The app searches when someone presses search, never per keystroke.
`geocode_url` can point at a self-hosted Nominatim or Photon later without
any client change.
"""
import asyncio
import time
from collections import OrderedDict
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.deps import require_observer
from app.config import settings
from app.ratelimit import Limit, check

router = APIRouter()

CACHE_SIZE = 2_000
CACHE_TTL_S = 7 * 24 * 3600
MAX_RESULTS = 5
# Half a degree is ~55 km: enough to prefer "MG Road" near the person over the
# one in another city, without hiding the other city if that's what they meant.
BIAS_DEGREES = 0.5

_cache: OrderedDict[tuple, tuple[float, list[dict]]] = OrderedDict()
_lock = asyncio.Lock()
_last_request = 0.0


def search_limit() -> Limit:
    return Limit(settings.rl_places_times, settings.rl_places_window_s)


def reset() -> None:
    global _last_request
    _cache.clear()
    _last_request = 0.0


def _place(item: dict) -> dict | None:
    try:
        lat, lng = float(item["lat"]), float(item["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    label = str(item.get("display_name") or "").strip()
    if not label:
        return None
    # The full display name runs to the country and the PIN code; the first
    # few parts are what tells two results apart on a phone.
    parts = [p.strip() for p in label.split(",") if p.strip()]
    return {"name": parts[0], "detail": ", ".join(parts[1:4]), "lat": lat, "lng": lng}


async def _ask(q: str, near: tuple[float, float] | None) -> list[dict]:
    params = {"q": q, "format": "jsonv2", "limit": str(MAX_RESULTS), "accept-language": "en"}
    if near is not None:
        lat, lng = near
        params["viewbox"] = (f"{lng - BIAS_DEGREES},{lat + BIAS_DEGREES},"
                             f"{lng + BIAS_DEGREES},{lat - BIAS_DEGREES}")
        params["bounded"] = "0"  # prefer nearby, don't exclude elsewhere
    async with httpx.AsyncClient(trust_env=False, timeout=settings.geocode_timeout_s) as client:
        response = await client.get(settings.geocode_url, params=params,
                                    headers={"User-Agent": settings.geocode_user_agent})
    response.raise_for_status()
    found = response.json()
    if not isinstance(found, list):
        raise ValueError("unexpected geocoder reply")
    return [p for p in (_place(item) for item in found if isinstance(item, dict)) if p]


@router.get("/places")
async def search_places(
    q: str = Query(min_length=2, max_length=200),
    lat: float | None = Query(None, ge=-90, le=90),
    lng: float | None = Query(None, ge=-180, le=180),
    observer_id: UUID = Depends(require_observer),
):
    global _last_request
    check(f"obs:{observer_id}:places", search_limit())
    query = " ".join(q.split())
    near = (lat, lng) if lat is not None and lng is not None else None
    # Rounded so that nudging the map a little still hits the cache.
    key = (query.casefold(), None if near is None else (round(near[0], 1), round(near[1], 1)))
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and now - hit[0] < CACHE_TTL_S:
        _cache.move_to_end(key)
        return {"places": hit[1]}
    async with _lock:
        # Nominatim's policy: one request a second, from this whole service.
        wait = _last_request + 1.0 - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            places = await _ask(query, near)
        except (httpx.HTTPError, ValueError):
            raise HTTPException(503, "place search is unavailable right now")
        finally:
            _last_request = time.monotonic()
    _cache[key] = (time.monotonic(), places)
    while len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)
    return {"places": places}
