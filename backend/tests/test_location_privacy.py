"""Nothing derived from someone else's sighting may say more than its cell.

`app/precision.py` coarsens the coordinate a viewer is shown. That protects
nothing if a decision made on the exact point leaks out some other way: whether
a sighting is close enough to be offered as a match, which ward it is counted
in, or a distance handed back beside it. These tests pin each of those to the
cell, the same way `/map`'s area filter is pinned in test_map.py.
"""

import math
import random

import pytest

from app.aggregates import area_rows
from app.config import settings
from app.ids import uuid7
from app.matching import find_candidates
from app.precision import _M_PER_DEG_LAT, coarsen
from app.social import area_name
from tests.test_matching import _observer, _sighting, _unit

BLR = (12.9716, 77.5946)


def _cell(lat, lng):
    """South-west corner and size in degrees of the cell holding (lat, lng)."""
    cell_m = settings.map_coarsen_cell_m
    dlat = cell_m / _M_PER_DEG_LAT
    south = math.floor(lat / dlat) * dlat
    dlng = cell_m / (_M_PER_DEG_LAT * math.cos(math.radians(south + dlat / 2)))
    return (math.floor(lng / dlng) * dlng, south), (dlng, dlat)


# --- coarse_point() ----------------------------------------------------------


async def test_sql_coarse_point_agrees_with_python_coarsen(migrated_db):
    """Two implementations of one rule. If they drift, a query decides on one
    cell while the response shows another."""
    rng = random.Random(146)
    points = [BLR, (0.0, 0.0), (-33.8688, 151.2093), (64.1, -21.9), (89.99, 10.0)]
    points += [(rng.uniform(-85, 85), rng.uniform(-179, 179)) for _ in range(300)]
    for cell_m in (settings.map_coarsen_cell_m, 250.0):
        rows = await migrated_db.fetch(
            "SELECT ST_Y(c::geometry) AS lat, ST_X(c::geometry) AS lng FROM ("
            " SELECT coarse_point(ST_SetSRID(ST_MakePoint(x, y), 4326)::geography, $3) AS c"
            " FROM unnest($1::float8[], $2::float8[]) AS t(y, x)) q",
            [p[0] for p in points], [p[1] for p in points], cell_m,
        )
        for (lat, lng), row in zip(points, rows):
            assert (row["lat"], row["lng"]) == pytest.approx(coarsen(lat, lng, cell_m), abs=1e-9)


# --- re-identification vicinity -----------------------------------------------


async def test_candidacy_depends_on_the_candidates_cell_not_its_point(migrated_db):
    """Whether a sighting is offered as a candidate is visible to whoever
    uploaded the query, and they chose its location. So two candidates in one
    cell must be offered together or not at all, from wherever the query is."""
    obs = await _observer(migrated_db)
    vec = _unit(146)
    (w, s), (dlng, dlat) = _cell(*BLR)
    a = await _sighting(migrated_db, obs, vec, lat=s + 0.15 * dlat, lng=w + 0.2 * dlng)
    b = await _sighting(migrated_db, obs, vec, lat=s + 0.85 * dlat, lng=w + 0.75 * dlng)

    rng = random.Random(146)
    outcomes = set()
    for _ in range(60):
        q = (s + rng.uniform(-2.5, 3.5) * dlat, w + rng.uniform(-2.5, 3.5) * dlng)
        got = {c.sighting_id for c in await find_candidates(
            migrated_db, vec, lat=q[0], lng=q[1],
            radius_m=settings.reid_radius_m, limit=10,
        )}
        assert (a in got) == (b in got), f"split by a query at {q}"
        outcomes.add(a in got)

    assert outcomes == {True, False}, "the sweep must exercise both answers"


async def test_two_queries_in_one_cell_get_the_same_candidates(migrated_db):
    """The query side too. Which pairs exist is published to every observer
    through `/proposals`, so where in its cell the query sighting was taken
    must not change which candidates it is paired with."""
    obs = await _observer(migrated_db)
    vec = _unit(151)
    (w, s), (dlng, dlat) = _cell(*BLR)
    rng = random.Random(151)
    for _ in range(45):
        await _sighting(migrated_db, obs, vec,
                        lat=s + rng.uniform(-3, 4) * dlat, lng=w + rng.uniform(-3, 4) * dlng)

    sets = []
    for fy, fx in ((0.05, 0.05), (0.95, 0.95), (0.05, 0.95), (0.95, 0.05), (0.5, 0.5)):
        sets.append({c.sighting_id for c in await find_candidates(
            migrated_db, vec, lat=s + fy * dlat, lng=w + fx * dlng,
            radius_m=settings.reid_radius_m, limit=50)})

    assert all(got == sets[0] for got in sets)
    assert sets[0], "the sample must put someone in reach"


async def test_no_candidate_the_exact_radius_admitted_is_lost(migrated_db):
    """Measured between cell centres with a half-diagonal added for each end,
    the candidate set can only grow: recall cannot drop."""
    obs = await _observer(migrated_db)
    vec = _unit(147)
    rng = random.Random(147)
    radius = settings.reid_radius_m
    spots = [(BLR[0] + rng.uniform(-0.025, 0.025), BLR[1] + rng.uniform(-0.025, 0.025))
             for _ in range(40)]
    placed = {await _sighting(migrated_db, obs, vec, lat=p[0], lng=p[1]): p for p in spots}

    for q in [BLR] + spots[:5]:
        exact = {
            sid for sid, p in placed.items()
            if await migrated_db.fetchval(
                "SELECT ST_DWithin(ST_SetSRID(ST_MakePoint($1,$2),4326)::geography,"
                " ST_SetSRID(ST_MakePoint($3,$4),4326)::geography, $5)",
                p[1], p[0], q[1], q[0], radius)
        }
        got = {c.sighting_id for c in await find_candidates(
            migrated_db, vec, lat=q[0], lng=q[1], radius_m=radius, limit=50)}
        assert exact <= got
        assert exact, "the sample must put someone inside the radius"


async def test_candidate_distance_is_to_the_cell_not_the_point(migrated_db):
    obs = await _observer(migrated_db)
    vec = _unit(148)
    (w, s), (dlng, dlat) = _cell(*BLR)
    await _sighting(migrated_db, obs, vec, lat=s + 0.1 * dlat, lng=w + 0.1 * dlng)
    centre = coarsen(s + 0.1 * dlat, w + 0.1 * dlng, settings.map_coarsen_cell_m)

    [c] = await find_candidates(migrated_db, vec, lat=centre[0], lng=centre[1],
                                radius_m=settings.reid_radius_m, limit=5)

    assert c.distance_m == pytest.approx(0.0, abs=0.5)


# --- GET /sighting/{id}/match --------------------------------------------------


async def _embedded_sighting(client, observer_id, vec, lat, lng):
    async with client._transport.app.state.pool.acquire() as conn:
        return await _sighting(conn, observer_id, vec, lat=lat, lng=lng)


async def test_match_is_for_the_sightings_owner_only(authed_client):
    client, mine = authed_client
    async with client._transport.app.state.pool.acquire() as conn:
        theirs_oid = await _observer(conn)
    vec = _unit(149)
    own = await _embedded_sighting(client, mine, vec, *BLR)
    theirs = await _embedded_sighting(client, theirs_oid, vec, BLR[0] + 0.001, BLR[1])

    r_own = await client.get(f"/sighting/{own}/match")
    r_theirs = await client.get(f"/sighting/{theirs}/match")
    r_none = await client.get(f"/sighting/{uuid7()}/match")

    assert r_own.status_code == 200
    assert r_theirs.status_code == r_none.status_code == 404
    assert r_theirs.json() == r_none.json(), "someone else's must look like one that does not exist"


async def test_match_gives_the_owner_candidates_but_no_distance(authed_client):
    """The uploader's own flow keeps working: candidates still come back. They
    carry no distance -- the query point is the uploader's to choose, so a
    distance to someone else's sighting would measure where it is."""
    client, mine = authed_client
    async with client._transport.app.state.pool.acquire() as conn:
        theirs_oid = await _observer(conn)
    vec = _unit(150)
    own = await _embedded_sighting(client, mine, vec, *BLR)
    theirs = await _embedded_sighting(client, theirs_oid, vec, BLR[0] + 0.001, BLR[1])

    body = (await client.get(f"/sighting/{own}/match")).json()

    assert [c["sighting_id"] for c in body["candidates"]] == [str(theirs)]
    assert all("distance_m" not in c for c in body["candidates"])


# --- ward attribution -----------------------------------------------------------


async def _ward(conn, name, x0, y0, x1, y1):
    aid = uuid7()
    await conn.execute(
        "INSERT INTO areas (id, name, kind, ext_code, geog) VALUES ($1, $2, $3, $2, "
        f"ST_GeogFromText('MULTIPOLYGON((({x0} {y0}, {x0} {y1}, {x1} {y1}, "
        f"{x1} {y0}, {x0} {y0})))'))",
        aid, name, settings.social_area_kind,
    )
    return aid


async def _located(conn, observer, lat, lng):
    sid = uuid7()
    await conn.execute(
        "INSERT INTO sightings (id, observer_id, captured_at, geog) VALUES "
        "($1, $2, now(), ST_SetSRID(ST_MakePoint($3, $4), 4326)::geography)",
        sid, observer, lng, lat,
    )
    return sid


async def test_a_ward_boundary_through_a_cell_does_not_split_it(migrated_db):
    """Counted by its exact point, which side of a boundary a sighting lands on
    places it within its cell. Counted by its cell, both sides land together
    -- in the ward holding the cell centre -- for /stats and for the caption a
    shared photo is posted with."""
    (w, s), (dlng, dlat) = _cell(*BLR)
    split = w + 0.4 * dlng  # the cell centre, at 0.5, is east of this
    west = await _ward(migrated_db, "West", w - 1, s - 1, split, s + 1)
    east = await _ward(migrated_db, "East", split, s - 1, w + 1, s + 1)
    obs = await _observer(migrated_db)
    a = await _located(migrated_db, obs, s + 0.5 * dlat, w + 0.2 * dlng)
    b = await _located(migrated_db, obs, s + 0.5 * dlat, w + 0.7 * dlng)

    counted = {r["id"]: r["sightings"] for r in await area_rows(migrated_db, settings.social_area_kind)}

    assert counted == {east: 2}
    assert west not in counted
    assert await area_name(migrated_db, a) == await area_name(migrated_db, b) == "East"


# --- GET /proposals ---------------------------------------------------------------


async def test_proposals_pair_two_uploads_in_one_cell_alike(authed_client):
    """`/proposals` names both sightings of a pair to every observer. Two
    uploads in one cell -- one near the edge facing an earlier sighting, one at
    the far edge -- must be paired with it alike, or which pairs exist says
    where in the cell each upload was taken."""
    from app.matching import resolve_sighting

    client, viewer = authed_client
    vec = _unit(152)
    (w, s), (dlng, dlat) = _cell(*BLR)
    pool = client._transport.app.state.pool
    async with pool.acquire() as conn:
        uploader = await _observer(conn)
        # The viewer's own earlier sighting, in the middle of its cell.
        earlier = await _sighting(conn, viewer, vec, lat=s + 0.5 * dlat, lng=w + 0.5 * dlng)
        # Two uploads in the cell two to the east: ~1.6 km and ~2.4 km from
        # the earlier sighting's cell centre, either side of where an exact
        # query point once drew the line.
        near = await _sighting(conn, uploader, vec, lat=s + 0.5 * dlat, lng=w + 2.1 * dlng)
        far = await _sighting(conn, uploader, vec, lat=s + 0.5 * dlat, lng=w + 2.9 * dlng)
        for sid in (near, far):
            await resolve_sighting(
                conn, sid, auto_merge_min=1.01, propose_min=0.5,
                radius_m=settings.reid_radius_m, max_candidates=5, new_uuid=uuid7,
            )

    pairs = {
        frozenset((p["a"]["sighting_id"], p["b"]["sighting_id"]))
        for p in (await client.get("/proposals")).json()["proposals"]
    }
    with_earlier = {frozenset((str(q), str(earlier))) in pairs for q in (near, far)}

    assert len(with_earlier) == 1, "one upload was paired with it and the other was not"
    assert with_earlier == {True}, "both are in reach of it, cell to cell"
