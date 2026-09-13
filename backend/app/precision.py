"""How precisely a viewer is told where a dog is.

THE THREAT THIS IS ABOUT
------------------------
This is not a generic privacy control. Free-roaming dogs are killed, poisoned
and taken, and a map that resolves a specific animal to a specific street is
directly useful to someone who wants to do that. The corpus is small enough
that a determined reader could work out a dog's routine from a handful of pins.

So the rule is: you get full precision for animals you have personally
photographed, and an area for everyone else's. That is standing earned by
contribution, the same model `POST /proposal/{id}` uses to decide who may merge
two sightings, and it needs no new schema -- `observer_id` has always been on
the row.

WHY A GRID CELL AND NOT A JITTERED POINT
----------------------------------------
The obvious version adds random noise to each coordinate. It does not work, and
it fails in a way that looks fine in testing:

* **Random jitter averages away.** Each fetch draws fresh noise, so a reader who
  requests the map ten times gets ten samples around the true point and the
  mean converges on it. The protection is undone by pressing refresh.
* **Even fixed per-sighting jitter leaks under aggregation.** Several sightings
  of one dog are several independent draws around its territory, so the more
  evidence there is about an animal, the better a reader can locate it. The
  dogs best documented -- the ones a hunter would most want -- are the ones
  least protected. That is exactly backwards.

Snapping to a fixed grid has neither property. The output is a function of the
input alone, so refreshing changes nothing, and every sighting inside one cell
returns the *identical* coordinate, so ten sightings of a dog say precisely what
one says: it is somewhere in this cell. There is nothing to average.

The cell centre is returned rather than the cell's corner or a label, because
the client is a map and has to draw something. It should be drawn as an area,
not a pin -- a pin at the cell centre reads as a claim about the centre, which
is the one place in the cell the dog demonstrably is not.

WHAT THIS IS NOT
----------------
Issue #5 settles that the eventual *public* surface resolves a dog to a named
`area` polygon -- a neighbourhood a person would recognise -- rather than to any
coordinate. That needs area polygons that do not exist yet. This is the
intermediate that can ship today, and it shares the important property: no
amount of repetition or aggregation sharpens it.

It also only coarsens *space*. Issue #5's other two dials, delaying recent
sightings and withholding identifying markings, are not implemented here.
"""

import math
from dataclasses import dataclass

# Metres per degree of latitude. Constant enough anywhere on Earth for this --
# the point is to make a cell roughly the requested size, not to survey it.
_M_PER_DEG_LAT = 111_320.0

# Longitude degrees shrink with cos(latitude), which reaches zero at the poles
# and would ask for an infinitely wide cell. Clamped so the arithmetic stays
# finite; at Bangalore's latitude cos is about 0.976 and this never binds.
_MIN_COS_LAT = 0.01


@dataclass(frozen=True)
class Located:
    """A coordinate and an honest statement of what it means.

    `precision` travels with the numbers on purpose. A client that receives a
    coarsened point and no indication of it will draw a pin, and a pin is a
    claim of exactness the server did not make.
    """

    lat: float
    lng: float
    precision: str  # "exact" | "area"
    # None when exact. The radius a client should draw, and what copy should
    # say, so the two cannot drift apart.
    cell_m: float | None = None


def coarsen(lat: float, lng: float, cell_m: float) -> tuple[float, float]:
    """Snap a coordinate to the centre of its grid cell.

    Deterministic: the same input always yields the same output, and every
    input inside one cell yields the same output. Those two properties are the
    whole protection -- see the module docstring on why jitter has neither.
    """
    if cell_m <= 0:
        return lat, lng

    lat_step = cell_m / _M_PER_DEG_LAT
    lat_centre = (math.floor(lat / lat_step) + 0.5) * lat_step

    # The longitude step is computed from the SNAPPED latitude, not the input
    # one. Using the input would make the cell's width depend on where inside
    # the cell the point fell, so two points in the same cell could land in
    # different longitude bands -- and then the pair of them would say more
    # than either alone, which is the leak this exists to close.
    cos_lat = max(math.cos(math.radians(lat_centre)), _MIN_COS_LAT)
    lng_step = cell_m / (_M_PER_DEG_LAT * cos_lat)
    lng_centre = (math.floor(lng / lng_step) + 0.5) * lng_step

    # Rounded to the metre-ish. Sixteen decimal places on a coordinate that is
    # accurate to a kilometre invites the reader to believe the digits.
    return round(lat_centre, 5), round(lng_centre, 5)


def resolve_precision(
    lat: float | None,
    lng: float | None,
    *,
    viewer_contributed: bool,
    cell_m: float,
) -> Located | None:
    """The one place that decides what a viewer is told.

    `viewer_contributed` is the standing test: did this person photograph this
    animal. Everything else about who they are is deliberately not consulted --
    a role or a trust tier would be a permission granted by a form, and the
    thing that actually makes full precision safe here is that the viewer was
    already standing next to the dog.

    Returns None when there is no location at all, which a sighting is allowed
    to have; the caller must not invent one.
    """
    if lat is None or lng is None:
        return None
    if viewer_contributed:
        return Located(lat=lat, lng=lng, precision="exact")
    clat, clng = coarsen(lat, lng, cell_m)
    return Located(lat=clat, lng=clng, precision="area", cell_m=cell_m)
