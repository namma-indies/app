"""The first surface a stranger could safely read.

Cohort-gated today. The properties tested here are the ones that have to hold
before that gate comes off: a thin area is indistinguishable from one that
does not exist, and the numbers reconcile in public.
"""

from datetime import datetime, timezone

from app.ids import uuid7


async def _pool(client):
    """Seed through the app's own pool, never via the `migrated_db` fixture.

    `migrated_db` TRUNCATEs on setup and so does `app_client`. A test that
    asks for both gets `migrated_db`'s truncation *after* `authed_client` has
    inserted the observer whose session it is using -- deleting that observer,
    and turning every subsequent request into a 401 that looks like an auth
    bug. One connection source per test removes the ordering question.
    """
    return client._transport.app.state.pool


async def _seed(client, *, areas=(), sightings=()):
    pool = await _pool(client)
    async with pool.acquire() as conn:
        for name, kind, code, x0, y0, size in areas:
            x1, y1 = x0 + size, y0 + size
            await conn.execute(
                f"INSERT INTO areas (id, name, kind, ext_code, geog) VALUES ($1, $2, $3, $4, "
                f"ST_GeogFromText('MULTIPOLYGON((({x0} {y0}, {x0} {y1}, {x1} {y1}, "
                f"{x1} {y0}, {x0} {y0})))'))",
                uuid7(), name, kind, code,
            )
        for observer_name, lat, lng, when in sightings:
            oid = await conn.fetchval(
                "SELECT id FROM observers WHERE display_name = $1", observer_name
            )
            if oid is None:
                oid = uuid7()
                await conn.execute(
                    "INSERT INTO observers (id, display_name) VALUES ($1, $2)",
                    oid, observer_name,
                )
            geog = "NULL" if lat is None else f"ST_GeogFromText('POINT({lng} {lat})')"
            await conn.execute(
                f"INSERT INTO sightings (id, observer_id, captured_at, geog) "
                f"VALUES ($1, $2, $3, {geog})",
                uuid7(), oid,
                datetime.fromisoformat(when).replace(tzinfo=timezone.utc),
            )


async def _one_area_id(client):
    pool = await _pool(client)
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT id FROM areas")


async def test_stats_requires_a_session(app_client):
    for path in ("/stats", "/stats/areas"):
        assert (await app_client.get(path)).status_code == 401


async def test_city_totals_are_never_suppressed(authed_client):
    """A one-sighting corpus still returns real numbers: this names no place,
    so there is no small cell to protect."""
    client, _ = authed_client
    await _seed(client, sightings=[("solo", 12.9, 77.6, "2026-08-01")])
    body = (await client.get("/stats")).json()
    assert body["totals"]["sightings"] == 1


async def test_a_thin_area_is_omitted_and_counted(authed_client):
    client, _ = authed_client
    await _seed(
        client,
        areas=[("Thin", "bbmp_ward", "1", 77.0, 12.0, 1.0)],
        sightings=[("a", 12.5, 77.5, "2026-08-01")],
    )
    body = (await client.get("/stats/areas?kind=bbmp_ward")).json()
    assert body["areas"] == []
    assert body["areas_suppressed"] == 1


async def test_an_area_over_both_thresholds_is_reported(authed_client):
    client, _ = authed_client
    await _seed(
        client,
        areas=[("Domlur", "bbmp_ward", "112", 77.0, 12.0, 1.0)],
        sightings=[
            ("a", 12.5, 77.5, "2026-08-01"), ("a", 12.5, 77.5, "2026-08-02"),
            ("a", 12.5, 77.5, "2026-08-03"), ("b", 12.5, 77.5, "2026-08-04"),
            ("b", 12.5, 77.5, "2026-08-05"),
        ],
    )
    body = (await client.get("/stats/areas?kind=bbmp_ward")).json()
    assert len(body["areas"]) == 1
    area = body["areas"][0]
    assert area["name"] == "Domlur"
    assert area["ext_code"] == "112"
    assert area["sightings"] == 5
    assert area["observers"] == 2
    assert area["last_active_month"] == "2026-08"


async def test_unattributed_sightings_are_reported(authed_client):
    client, _ = authed_client
    await _seed(
        client,
        areas=[("Domlur", "bbmp_ward", "112", 77.0, 12.0, 1.0)],
        sightings=[("a", None, None, "2026-08-01"), ("a", 50.0, 10.0, "2026-08-02")],
    )
    body = (await client.get("/stats/areas?kind=bbmp_ward")).json()
    assert body["unattributed_sightings"] == 2


async def test_a_suppressed_area_by_id_is_a_404(authed_client):
    """Identical to a nonexistent id on purpose. A distinct 403 would turn
    the endpoint into an oracle for the fact suppression exists to withhold."""
    client, _ = authed_client
    await _seed(
        client,
        areas=[("Thin", "bbmp_ward", "1", 77.0, 12.0, 1.0)],
        sightings=[("a", 12.5, 77.5, "2026-08-01")],
    )
    area_id = await _one_area_id(client)
    assert (await client.get(f"/stats/areas/{area_id}")).status_code == 404
    assert (await client.get(f"/stats/areas/{uuid7()}")).status_code == 404


async def test_a_thin_month_is_dropped_from_an_areas_series(authed_client):
    """An area-month cell is a finer disclosure than the area row above it,
    so it is floored separately. Totals still include the dropped month."""
    client, _ = authed_client
    await _seed(
        client,
        areas=[("Domlur", "bbmp_ward", "112", 77.0, 12.0, 1.0)],
        sightings=[
            ("a", 12.5, 77.5, "2026-08-01"), ("a", 12.5, 77.5, "2026-08-02"),
            ("a", 12.5, 77.5, "2026-08-03"), ("b", 12.5, 77.5, "2026-08-04"),
            ("b", 12.5, 77.5, "2026-08-05"),
            ("b", 12.5, 77.5, "2026-09-01"),   # a lone September
        ],
    )
    area_id = await _one_area_id(client)
    body = (await client.get(f"/stats/areas/{area_id}")).json()
    assert [m["month"] for m in body["months"]] == ["2026-08"]
    assert body["area"]["sightings"] == 6


async def test_no_polygons_loaded_is_an_honest_empty_not_a_404(authed_client):
    client, _ = authed_client
    await _seed(client, sightings=[("a", 12.5, 77.5, "2026-08-01")])
    resp = await client.get("/stats/areas?kind=nothing_loaded")
    assert resp.status_code == 200
    body = resp.json()
    assert body["areas"] == []
    assert body["unattributed_sightings"] == 1


async def test_kind_defaults_to_config(authed_client):
    from app.config import settings

    client, _ = authed_client
    body = (await client.get("/stats/areas")).json()
    assert body["kind"] == settings.area_default_kind
