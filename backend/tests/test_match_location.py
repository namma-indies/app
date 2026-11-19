"""Where each side of a match pair was taken, and no more precisely than /map.

The pair card shows both sightings on a small map with the distance between
them. The rule is the one in app/precision.py: the viewer's own sighting
exactly, anyone else's at its grid cell -- the identical coordinate /map gives
the same viewer -- and nothing for a sighting /map would not show. The distance
is between the coordinates shown, so it cannot be used to place another
observer's sighting inside its cell.
"""

import json
import math

import pytest

from app.config import settings
from app.embed import MODEL_NAME
from app.ids import uuid7
from app.precision import Located, _M_PER_DEG_LAT, coarsen, distance_m

BLR = (12.9716, 77.5946)


def _cell(lat, lng):
    """South-west corner and size in degrees of the cell holding (lat, lng)."""
    cell_m = settings.map_coarsen_cell_m
    dlat = cell_m / _M_PER_DEG_LAT
    south = math.floor(lat / dlat) * dlat
    dlng = cell_m / (_M_PER_DEG_LAT * math.cos(math.radians(south + dlat / 2)))
    return (math.floor(lng / dlng) * dlng, south), (dlng, dlat)


async def _observer(conn):
    oid = uuid7()
    await conn.execute("INSERT INTO observers (id, display_name) VALUES ($1, 'Other')", oid)
    return oid


async def _sighting(conn, oid, lat=None, lng=None):
    sid = uuid7()
    if lat is None:
        await conn.execute(
            "INSERT INTO sightings (id, observer_id, captured_at) VALUES ($1, $2, now())", sid, oid)
    else:
        await conn.execute(
            "INSERT INTO sightings (id, observer_id, captured_at, geog) VALUES "
            "($1, $2, now(), ST_SetSRID(ST_MakePoint($3, $4), 4326)::geography)",
            sid, oid, lng, lat)
    await conn.execute("INSERT INTO photos (id, sighting_id, s3_key) VALUES ($1, $2, $3)",
                       uuid7(), sid, f"k/{sid}.webp")
    return sid


async def _proposal(conn, a, b):
    pid = uuid7()
    await conn.execute(
        "INSERT INTO match_proposals (id, sighting_id, candidate_sighting_id, score, method) "
        "VALUES ($1, $2, $3, 0.6, $4)", pid, a, b, MODEL_NAME)
    return pid


async def _card(client, pid):
    response = await client.get("/proposals")
    assert response.status_code == 200
    (card,) = [p for p in response.json()["proposals"] if p["id"] == str(pid)]
    return card, response.text


def _digits(x: float) -> list[str]:
    """Spellings of an exact coordinate that would betray it in a response."""
    return [repr(x), f"{x:.6f}", f"{x:.5f}"]


async def test_own_side_is_exact_and_the_other_is_its_map_cell(authed_client):
    client, viewer = authed_client
    (w, s), (dlng, dlat) = _cell(*BLR)
    own_at = (s + 0.5 * dlat, w + 0.5 * dlng)
    # Two cells east, deliberately off-centre in its cell.
    theirs_at = (s + 0.17 * dlat, w + 2.83 * dlng)
    async with client._transport.app.state.pool.acquire() as conn:
        other = await _observer(conn)
        own = await _sighting(conn, viewer, *own_at)
        theirs = await _sighting(conn, other, *theirs_at)
        pid = await _proposal(conn, own, theirs)

    card, raw = await _card(client, pid)

    assert card["a"]["location"] == {"lat": own_at[0], "lng": own_at[1],
                                     "precision": "exact", "cell_m": None}
    clat, clng = coarsen(*theirs_at, settings.map_coarsen_cell_m)
    assert card["b"]["location"] == {"lat": clat, "lng": clng, "precision": "area",
                                     "cell_m": settings.map_coarsen_cell_m}
    for spelling in _digits(theirs_at[0]) + _digits(theirs_at[1]):
        assert spelling not in raw, "another observer's exact coordinate left the server"

    shown = distance_m(Located(*own_at, "exact"), Located(clat, clng, "area"))
    assert card["distance_m"] == shown
    exact = distance_m(Located(*own_at, "exact"), Located(*theirs_at, "exact"))
    assert shown != exact, "fixture must tell the shown distance from the exact one"


async def test_the_other_side_matches_what_map_shows_this_viewer(authed_client):
    client, viewer = authed_client
    (w, s), (dlng, dlat) = _cell(*BLR)
    async with client._transport.app.state.pool.acquire() as conn:
        other = await _observer(conn)
        own = await _sighting(conn, viewer, s + 0.4 * dlat, w + 0.4 * dlng)
        theirs = await _sighting(conn, other, s + 0.91 * dlat, w + 1.08 * dlng)
        pid = await _proposal(conn, own, theirs)

    card, _ = await _card(client, pid)
    on_map = {p["id"]: p for p in (await client.get("/map")).json()["sightings"]}

    for side in ("a", "b"):
        loc, pin = card[side]["location"], on_map[card[side]["sighting_id"]]
        assert (loc["lat"], loc["lng"], loc["precision"], loc["cell_m"]) == (
            pin["lat"], pin["lng"], pin["precision"], pin["cell_m"])


async def test_neither_side_is_exact_when_neither_is_the_viewers(authed_client):
    client, _ = authed_client
    (w, s), (dlng, dlat) = _cell(*BLR)
    points = [(s + 0.2 * dlat, w + 0.3 * dlng), (s + 0.8 * dlat, w + 1.6 * dlng)]
    async with client._transport.app.state.pool.acquire() as conn:
        o1, o2 = await _observer(conn), await _observer(conn)
        a = await _sighting(conn, o1, *points[0])
        b = await _sighting(conn, o2, *points[1])
        pid = await _proposal(conn, a, b)

    card, raw = await _card(client, pid)

    for side, at in zip(("a", "b"), points):
        loc = card[side]["location"]
        assert loc["precision"] == "area"
        assert (loc["lat"], loc["lng"]) == coarsen(*at, settings.map_coarsen_cell_m)
        for spelling in _digits(at[0]) + _digits(at[1]):
            assert spelling not in raw
    a_loc, b_loc = card["a"]["location"], card["b"]["location"]
    assert card["distance_m"] == distance_m(Located(a_loc["lat"], a_loc["lng"], "area"),
                                            Located(b_loc["lat"], b_loc["lng"], "area"))


@pytest.mark.parametrize("fraction", [(0.05, 0.05), (0.5, 0.5), (0.95, 0.2), (0.3, 0.97)])
async def test_nothing_depends_on_where_in_its_cell_the_other_sighting_is(authed_client, fraction):
    """Location and distance must be the same from anywhere in the cell, or
    the card is a way to learn what the cell withholds."""
    client, viewer = authed_client
    (w, s), (dlng, dlat) = _cell(*BLR)
    async with client._transport.app.state.pool.acquire() as conn:
        other = await _observer(conn)
        own = await _sighting(conn, viewer, s + 0.5 * dlat, w + 0.5 * dlng)
        # A cell one row north and two columns east, read off its own row (a
        # cell's width in longitude depends on its latitude).
        (tw, ts), (tdlng, tdlat) = _cell(s + 1.5 * dlat, w + 2.5 * dlng)
        theirs = await _sighting(conn, other, ts + fraction[0] * tdlat, tw + fraction[1] * tdlng)
        pid = await _proposal(conn, own, theirs)

    card, _ = await _card(client, pid)

    centre = coarsen(ts + 0.5 * tdlat, tw + 0.5 * tdlng, settings.map_coarsen_cell_m)
    assert (card["b"]["location"]["lat"], card["b"]["location"]["lng"]) == centre
    assert card["distance_m"] == distance_m(
        Located(s + 0.5 * dlat, w + 0.5 * dlng, "exact"), Located(*centre, "area"))


async def test_a_sighting_off_the_map_has_no_location_on_the_card(authed_client):
    """A later copy of a photo is off /map (duplicate_of), though the pair
    filters let it through; the card must not show what the map does not."""
    client, viewer = authed_client
    async with client._transport.app.state.pool.acquire() as conn:
        other = await _observer(conn)
        own = await _sighting(conn, viewer, *BLR)
        original = await _sighting(conn, other, BLR[0] + 0.02, BLR[1])
        copy = await _sighting(conn, other, BLR[0] + 0.03, BLR[1] + 0.01)
        await conn.execute("UPDATE sightings SET duplicate_of = $1 WHERE id = $2", original, copy)
        pid = await _proposal(conn, own, copy)

    card, raw = await _card(client, pid)

    assert card["b"]["location"] is None
    assert card["distance_m"] is None
    assert card["a"]["location"]["precision"] == "exact"
    assert str(copy) not in {p["id"] for p in (await client.get("/map")).json()["sightings"]}
    assert "77.6046" not in raw and "13.0016" not in raw


async def test_own_duplicate_is_still_shown_exactly(authed_client):
    """The viewer's own sighting is theirs to see wherever it is (/dex)."""
    client, viewer = authed_client
    async with client._transport.app.state.pool.acquire() as conn:
        other = await _observer(conn)
        original = await _sighting(conn, viewer, *BLR)
        own_copy = await _sighting(conn, viewer, BLR[0] + 0.001, BLR[1])
        await conn.execute("UPDATE sightings SET duplicate_of = $1 WHERE id = $2", original, own_copy)
        theirs = await _sighting(conn, other, BLR[0] + 0.01, BLR[1])
        pid = await _proposal(conn, own_copy, theirs)

    card, _ = await _card(client, pid)

    assert card["a"]["location"]["precision"] == "exact"
    assert card["b"]["location"]["precision"] == "area"


async def test_unlocated_side_has_no_location_and_no_distance(authed_client):
    client, viewer = authed_client
    async with client._transport.app.state.pool.acquire() as conn:
        other = await _observer(conn)
        own = await _sighting(conn, viewer)
        theirs = await _sighting(conn, other, *BLR)
        pid = await _proposal(conn, own, theirs)

    card, _ = await _card(client, pid)

    assert card["a"]["location"] is None
    assert card["b"]["location"]["precision"] == "area"
    assert card["distance_m"] is None


async def test_both_own_sides_are_exact(authed_client):
    client, viewer = authed_client
    first, second = BLR, (BLR[0] + 0.0031, BLR[1] + 0.0042)
    async with client._transport.app.state.pool.acquire() as conn:
        a = await _sighting(conn, viewer, *first)
        b = await _sighting(conn, viewer, *second)
        pid = await _proposal(conn, a, b)

    card, _ = await _card(client, pid)

    assert card["a"]["location"]["precision"] == card["b"]["location"]["precision"] == "exact"
    assert card["distance_m"] == distance_m(Located(*first, "exact"), Located(*second, "exact"))


async def test_no_response_field_carries_an_unshown_coordinate(authed_client):
    """Belt and braces: walk the whole response for any float that equals an
    exact coordinate of another observer's sighting."""
    client, viewer = authed_client
    async with client._transport.app.state.pool.acquire() as conn:
        other = await _observer(conn)
        exact = (12.98317, 77.61129)
        own = await _sighting(conn, viewer, *BLR)
        theirs = await _sighting(conn, other, *exact)
        await _proposal(conn, own, theirs)
    body = (await client.get("/proposals")).json()

    def floats(x):
        if isinstance(x, float):
            yield x
        elif isinstance(x, dict):
            for v in x.values():
                yield from floats(v)
        elif isinstance(x, list):
            for v in x:
                yield from floats(v)

    seen = list(floats(body))
    assert all(abs(f - exact[0]) > 1e-6 and abs(f - exact[1]) > 1e-6 for f in seen), json.dumps(body)


def test_distance_is_rounded_to_ten_metres():
    a = Located(12.9716, 77.5946, "exact")
    b = Located(12.9716 + 1234 / _M_PER_DEG_LAT, 77.5946, "exact")
    assert distance_m(a, b) == 1230
    assert distance_m(a, a) == 0
