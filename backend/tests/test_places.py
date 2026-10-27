"""Place search for the pick-on-a-map screen (#80), with the geocoder faked."""
import httpx
import pytest

from app.config import settings
from app.routes import places

NOMINATIM_REPLY = [
    {"lat": "12.9601", "lon": "77.6387",
     "display_name": "5th Cross Road, Domlur, Bengaluru East City Corporation, Bengaluru, Karnataka, 560071, India"},
    {"lat": "not a number", "lon": "77.1", "display_name": "broken"},
    {"lat": "10.2381", "lon": "77.4892", "display_name": ""},
]


@pytest.fixture
def geocoder(monkeypatch):
    """Answers like Nominatim and records every request it was sent."""
    seen = []
    places.reset()

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=NOMINATIM_REPLY)

    real = httpx.AsyncClient

    def client(*args, **kwargs):
        return real(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(places.httpx, "AsyncClient", client)
    yield seen
    places.reset()


@pytest.mark.asyncio
async def test_search_returns_short_names_and_skips_unusable_results(authed_client, geocoder):
    client, _ = authed_client
    r = await client.get("/places", params={"q": "Domlur 5th cross"})
    assert r.status_code == 200
    assert r.json() == {"places": [{
        "name": "5th Cross Road",
        "detail": "Domlur, Bengaluru East City Corporation, Bengaluru",
        "lat": 12.9601, "lng": 77.6387,
    }]}
    request = geocoder[0]
    assert request.url.params["q"] == "Domlur 5th cross"
    assert request.url.params["format"] == "jsonv2"
    assert "NammaIndies" in request.headers["user-agent"]  # Nominatim requires identifying ourselves
    assert "viewbox" not in request.url.params


@pytest.mark.asyncio
async def test_prefers_places_near_where_the_map_is(authed_client, geocoder):
    client, _ = authed_client
    await client.get("/places", params={"q": "MG Road", "lat": 12.97, "lng": 77.59})
    params = geocoder[0].url.params
    assert params["viewbox"] == "77.09,13.47,78.09,12.47"
    assert params["bounded"] == "0"  # nearby first, elsewhere still found


@pytest.mark.asyncio
async def test_the_same_search_is_answered_from_cache(authed_client, geocoder):
    client, _ = authed_client
    for q in ("Domlur 5th cross", "domlur   5th CROSS"):
        assert (await client.get("/places", params={"q": q})).status_code == 200
    assert len(geocoder) == 1


@pytest.mark.asyncio
async def test_requests_to_the_geocoder_are_spaced_a_second_apart(authed_client, geocoder):
    import time
    client, _ = authed_client
    started = time.monotonic()
    await client.get("/places", params={"q": "Indiranagar"})
    await client.get("/places", params={"q": "Koramangala"})
    assert len(geocoder) == 2
    assert time.monotonic() - started >= 1.0


@pytest.mark.asyncio
async def test_a_geocoder_outage_is_a_503_not_a_crash(authed_client, monkeypatch):
    client, _ = authed_client
    places.reset()

    async def down(q, near):
        raise httpx.ConnectError("unreachable")

    monkeypatch.setattr(places, "_ask", down)
    r = await client.get("/places", params={"q": "Kodaikanal"})
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_search_needs_a_signed_in_observer(app_client):
    r = await app_client.get("/places", params={"q": "Kodaikanal"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_rejects_empty_or_huge_queries(authed_client, geocoder):
    client, _ = authed_client
    assert (await client.get("/places", params={"q": "x"})).status_code == 422
    assert (await client.get("/places", params={"q": "x" * 201})).status_code == 422
    assert not geocoder


@pytest.mark.asyncio
async def test_one_person_cannot_drain_the_shared_budget(authed_client, geocoder, monkeypatch):
    client, _ = authed_client
    monkeypatch.setattr(settings, "rl_places_times", 2)
    # The limiter counts per fixed clock window, and these three calls take
    # ~2 s (one geocoder call a second): a 60 s window could roll over between
    # them. One this long never does.
    monkeypatch.setattr(settings, "rl_places_window_s", 10**9)
    codes = [(await client.get("/places", params={"q": f"place {i}"})).status_code for i in range(3)]
    assert codes == [200, 200, 429]
