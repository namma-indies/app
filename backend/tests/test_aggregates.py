"""Derived counts, and the rules that keep them from becoming a location.

Three properties are load-bearing and each has a test here:
  - a sighting is counted once, even where polygons overlap;
  - a thin area is omitted entirely rather than flagged, because a flag still
    says "a dog is here";
  - the numbers reconcile -- per-area rows plus suppressed plus unattributed
    account for the city total.
"""

from datetime import datetime, timezone

from app.aggregates import (
    area_months,
    area_rows,
    city_months,
    city_totals,
    suppress,
    thresholds,
    unattributed_sightings,
)
from app.ids import uuid7


async def _observer(conn, name="o"):
    oid = uuid7()
    await conn.execute(
        "INSERT INTO observers (id, display_name) VALUES ($1, $2)", oid, name
    )
    return oid


async def _area(conn, name, kind, code, x0, y0, size=1.0):
    aid = uuid7()
    x1, y1 = x0 + size, y0 + size
    await conn.execute(
        f"INSERT INTO areas (id, name, kind, ext_code, geog) VALUES ($1, $2, $3, $4, "
        f"ST_GeogFromText('MULTIPOLYGON((({x0} {y0}, {x0} {y1}, {x1} {y1}, "
        f"{x1} {y0}, {x0} {y0})))'))",
        aid, name, kind, code,
    )
    return aid


async def _sighting(conn, observer, *, lat=None, lng=None, when="2026-08-15",
                    individual=None, review="valid"):
    sid = uuid7()
    geog = "NULL" if lat is None else f"ST_GeogFromText('POINT({lng} {lat})')"
    await conn.execute(
        f"INSERT INTO sightings (id, observer_id, captured_at, geog, individual_id, "
        f"review_status) VALUES ($1, $2, $3, {geog}, $4, $5)",
        sid, observer,
        datetime.fromisoformat(when).replace(tzinfo=timezone.utc),
        individual, review,
    )
    return sid


async def _individual(conn):
    iid = uuid7()
    await conn.execute("INSERT INTO individuals (id) VALUES ($1)", iid)
    return iid


async def test_three_numbers_are_counted_separately(migrated_db):
    """Sightings overcount dogs, confirmed individuals undercount them. The
    API returns both rather than one guess wearing a fact's clothes."""
    o1 = await _observer(migrated_db, "a")
    o2 = await _observer(migrated_db, "b")
    dog = await _individual(migrated_db)
    await _sighting(migrated_db, o1, lat=12.9, lng=77.6, individual=dog)
    await _sighting(migrated_db, o1, lat=12.9, lng=77.6, individual=dog)
    await _sighting(migrated_db, o2, lat=12.9, lng=77.6)  # unmatched

    totals = await city_totals(migrated_db)
    assert totals == {"observers": 2, "sightings": 3, "confirmed_individuals": 1}


async def test_rejected_sightings_count_nowhere(migrated_db):
    o = await _observer(migrated_db)
    await _sighting(migrated_db, o, lat=12.9, lng=77.6)
    await _sighting(migrated_db, o, lat=12.9, lng=77.6, review="rejected")
    assert (await city_totals(migrated_db))["sightings"] == 1


async def test_a_sighting_in_two_overlapping_areas_is_counted_once(migrated_db):
    """Without LIMIT 1 in the lateral join, per-area rows exceed the city
    total and the map appears to grow dogs."""
    o = await _observer(migrated_db)
    await _area(migrated_db, "A", "k", "1", 77.0, 12.0, size=2.0)
    await _area(migrated_db, "B", "k", "2", 77.5, 12.5, size=2.0)
    await _sighting(migrated_db, o, lat=13.0, lng=77.8)  # inside both

    rows = await area_rows(migrated_db, "k")
    assert sum(r["sightings"] for r in rows) == 1


async def test_sightings_outside_every_polygon_are_unattributed(migrated_db):
    o = await _observer(migrated_db)
    await _area(migrated_db, "A", "k", "1", 77.0, 12.0)
    await _sighting(migrated_db, o, lat=12.5, lng=77.5)   # inside
    await _sighting(migrated_db, o, lat=50.0, lng=10.0)   # outside every polygon
    await _sighting(migrated_db, o, lat=None, lng=None)   # geo_source 'none'

    assert await unattributed_sightings(migrated_db, "k") == 2


async def test_kinds_do_not_leak_into_each_other(migrated_db):
    o = await _observer(migrated_db)
    await _area(migrated_db, "Ward", "bbmp_ward", "1", 77.0, 12.0)
    await _area(migrated_db, "Pin", "pin_code", "560038", 77.0, 12.0)
    await _sighting(migrated_db, o, lat=12.5, lng=77.5)

    wards = await area_rows(migrated_db, "bbmp_ward")
    pins = await area_rows(migrated_db, "pin_code")
    assert [r["name"] for r in wards] == ["Ward"]
    assert [r["name"] for r in pins] == ["Pin"]
    assert wards[0]["sightings"] == pins[0]["sightings"] == 1


async def test_months_bucket_in_india_not_utc(migrated_db):
    """23:30 UTC on 31 August is 05:00 IST on 1 September. A Bangalore
    project that files that under August is reporting someone else's month."""
    o = await _observer(migrated_db)
    await _sighting(migrated_db, o, lat=12.9, lng=77.6, when="2026-08-31T23:30:00")
    months = await city_months(migrated_db)
    assert [m["month"] for m in months] == ["2026-09"]


async def test_nothing_finer_than_a_month_is_emitted(migrated_db):
    o = await _observer(migrated_db)
    await _sighting(migrated_db, o, lat=12.9, lng=77.6, when="2026-07-02")
    await _sighting(migrated_db, o, lat=12.9, lng=77.6, when="2026-07-20")
    await _sighting(migrated_db, o, lat=12.9, lng=77.6, when="2026-08-03")
    months = await city_months(migrated_db)
    assert [m["month"] for m in months] == ["2026-07", "2026-08"]
    assert months[0]["sightings"] == 2


async def test_suppression_needs_both_thresholds(migrated_db):
    """Plenty of sightings from a single observer is still one person's
    routine, which is the pattern the three dials exist to strip."""
    rows = [
        {"name": "thin", "sightings": 4, "observers": 3},
        {"name": "lonely", "sightings": 40, "observers": 1},
        {"name": "ok", "sightings": 5, "observers": 2},
    ]
    kept, suppressed = suppress(rows, "bbmp_ward")
    assert [r["name"] for r in kept] == ["ok"]
    assert suppressed == 2


async def test_unknown_kinds_fall_back_to_a_threshold(migrated_db):
    assert thresholds("something_new") == (5, 2)
    assert thresholds("pin_code") == (20, 3)


async def test_an_areas_month_series_matches_its_own_attribution(migrated_db):
    """The series must use the same lateral attribution as the area row, or
    an overlapping polygon gives an area months it was not credited with."""
    o = await _observer(migrated_db)
    a = await _area(migrated_db, "A", "k", "1", 77.0, 12.0, size=2.0)
    await _area(migrated_db, "B", "k", "2", 77.5, 12.5, size=2.0)
    await _sighting(migrated_db, o, lat=13.0, lng=77.8, when="2026-07-10")

    a_months = await area_months(migrated_db, a, "k")
    assert sum(m["sightings"] for m in a_months) == 1
