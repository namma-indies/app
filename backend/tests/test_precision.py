"""What a coarsened coordinate must guarantee.

These are not tests that the arithmetic rounds correctly. They are tests of the
two properties that make grid-snapping worth doing at all, both of which the
obvious alternative -- random jitter -- fails:

1. Repeating the request tells you nothing new.
2. Having more sightings of one dog tells you nothing new.

Jitter fails (1) because each fetch draws fresh noise, so ten fetches average to
the truth. It fails (2) because several sightings of one animal are several
independent draws around its territory, which means the best-documented dogs --
the ones worth finding -- are the least protected. That is backwards, and it is
invisible unless a test says so.
"""

import math

import pytest

from app.precision import coarsen, resolve_precision

BLR = (12.9716, 77.5946)
CELL = 1000.0


def _metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    dlat = (a[0] - b[0]) * 111_320.0
    dlng = (a[1] - b[1]) * 111_320.0 * math.cos(math.radians(a[0]))
    return math.hypot(dlat, dlng)


def test_repeating_the_request_reveals_nothing():
    """The failure jitter has. A reader who fetches the map a thousand times
    gets a thousand samples around the true point and averages them back to it.
    Here every call is the same call."""
    outs = {coarsen(*BLR, CELL) for _ in range(1000)}
    assert len(outs) == 1


def test_many_sightings_of_one_dog_reveal_nothing_more_than_one():
    """The failure that matters most. A dog seen twenty times inside one cell
    must produce twenty identical coordinates -- otherwise the animal a hunter
    would most want to find is the one the map describes best."""
    lat, lng = coarsen(*BLR, CELL)
    # Points scattered well inside the same cell, not near its edges.
    lat_step = CELL / 111_320.0
    lng_step = CELL / (111_320.0 * math.cos(math.radians(lat)))
    scattered = [
        (lat + dx * lat_step * 0.3, lng + dy * lng_step * 0.3)
        for dx in (-1, 0, 1)
        for dy in (-1, 0, 1)
    ]
    outs = {coarsen(la, ln, CELL) for la, ln in scattered}
    assert len(outs) == 1, f"one cell produced {len(outs)} distinct answers"


def test_the_average_of_the_answers_is_not_the_true_point():
    """Stated directly, because 'it is deterministic' and 'averaging does not
    help' are different claims and only the second is the security property."""
    outs = [coarsen(*BLR, CELL) for _ in range(500)]
    mean = (sum(o[0] for o in outs) / len(outs), sum(o[1] for o in outs) / len(outs))
    assert _metres(mean, BLR) > 100


def test_coarsening_actually_moves_the_point():
    """A guard against a cell size so small, or an implementation so broken,
    that this becomes a no-op that still reports precision='area'."""
    assert _metres(coarsen(*BLR, CELL), BLR) > 50


def test_displacement_stays_inside_the_cell():
    """The claim made to the user is 'somewhere in a cell this size'. If the
    output can land further away than the cell's own diagonal, the drawn area
    would not contain the dog and the copy would be a lie."""
    diagonal = CELL * math.sqrt(2) / 2
    lat_step = CELL / 111_320.0
    for i in range(200):
        lat = 12.90 + i * lat_step / 7
        lng = 77.50 + i * lat_step / 5
        assert _metres(coarsen(lat, lng, CELL), (lat, lng)) <= diagonal + 1


def test_coarsening_is_idempotent():
    """Re-coarsening an already-coarse point must not drift it further. A
    coordinate can pass through this twice -- cached, re-served -- and each pass
    must not add another cell of error."""
    once = coarsen(*BLR, CELL)
    assert coarsen(*once, CELL) == once


def test_the_longitude_band_does_not_depend_on_where_in_the_cell_you_were():
    """The subtle one. Computing the longitude step from the *input* latitude
    rather than the snapped one makes a cell's width vary within itself, so two
    points in the same cell can land in different longitude bands -- and then
    the pair says more than either alone, which is the leak this exists to
    close."""
    lat_step = CELL / 111_320.0
    base_lat = (math.floor(BLR[0] / lat_step) + 0.5) * lat_step
    low = coarsen(base_lat - lat_step * 0.45, 77.5946, CELL)
    high = coarsen(base_lat + lat_step * 0.45, 77.5946, CELL)
    assert low == high


@pytest.mark.parametrize("lat,lng", [(0.0, 0.0), (-33.9, 151.2), (64.1, -21.9), (12.97, 179.9)])
def test_works_away_from_bangalore(lat, lng):
    out = coarsen(lat, lng, CELL)
    assert _metres(out, (lat, lng)) <= CELL * math.sqrt(2) / 2 + 1


def test_a_zero_cell_is_a_no_op_rather_than_a_division_by_zero():
    """Setting map_coarsen_cell_m to 0 should mean 'do not coarsen', which is a
    legible way to turn this off, not a crash on every map request."""
    assert coarsen(*BLR, 0.0) == BLR


# --- the standing rule -------------------------------------------------------


def test_your_own_sightings_come_back_exactly():
    got = resolve_precision(*BLR, viewer_contributed=True, cell_m=CELL)
    assert (got.lat, got.lng) == BLR
    assert got.precision == "exact"
    assert got.cell_m is None


def test_someone_elses_are_coarsened_and_say_so():
    got = resolve_precision(*BLR, viewer_contributed=False, cell_m=CELL)
    assert (got.lat, got.lng) != BLR
    assert got.precision == "area"
    # The radius travels with the number so the client's copy and the server's
    # arithmetic cannot drift apart.
    assert got.cell_m == CELL


def test_a_sighting_with_no_location_stays_without_one():
    """A sighting is allowed to have no coordinate. Coarsening must not invent
    one -- a dog placed at the centre of cell (0,0) is off the coast of Africa."""
    assert resolve_precision(None, None, viewer_contributed=False, cell_m=CELL) is None
    assert resolve_precision(12.9, None, viewer_contributed=True, cell_m=CELL) is None
