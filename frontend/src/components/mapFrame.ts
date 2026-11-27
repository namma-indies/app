import type { MappableSighting } from "../api";

/**
 * Where the map opens.
 *
 * It used to fit every sighting at once. That is fine while everything you
 * have logged is in one part of town, and stops being fine the first time you
 * log a dog on a trip: one sighting in Goa puts Bangalore and Goa in the same
 * frame, and on the shared map one in Berkeley makes it a map of the world
 * with a paw bubble on each continent. Nobody reads a census at that scale.
 *
 * So it opens on one neighbourhood instead, and the rest is a pinch away:
 *
 * 1. Where you are standing, when the device will say so without asking and
 *    there is something logged around you.
 * 2. Otherwise where you last saw an animal. `captured_at` rather than upload
 *    order, so importing last year's holiday photos does not move the map.
 * 3. Otherwise (the shared map, before you have logged anything) wherever the
 *    most sightings are.
 */

export interface Place { lat: number; lng: number }

/** How far around the anchor still counts as the same neighbourhood. Matches
 * the 1 km the re-ID candidate search uses and the 1 km cells other people's
 * sightings are coarsened to, so the frame holds roughly the animals you could
 * plausibly be about to meet. On a phone this lands the map at around zoom 14. */
export const NEIGHBOURHOOD_M = 1000;

/** Cell size for finding the busiest area, in degrees: about 2 km of latitude. */
const BUSY_CELL_DEG = 0.02;

export type FrameAnchor = "here" | "mine" | "busiest" | "none";

export interface HomeFrame {
  /** What the frame was built around. `none` means there is nothing to frame. */
  anchor: FrameAnchor;
  /** `[lng, lat]` pairs to fit, the anchor first. */
  points: [number, number][];
}

/** Great-circle distance. Haversine rather than a flat approximation because
 * sightings really are on both sides of the antimeridian. */
export function metresBetween(a: Place, b: Place): number {
  const rad = Math.PI / 180;
  const dLat = (b.lat - a.lat) * rad;
  const dLng = (b.lng - a.lng) * rad;
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLng / 2) ** 2;
  return 2 * 6_371_000 * Math.asin(Math.min(1, Math.sqrt(h)));
}

type Located = MappableSighting & Place;

function when(s: MappableSighting): number {
  const t = Date.parse(s.captured_at);
  return Number.isNaN(t) ? -Infinity : t;
}

function latest(sightings: Located[]): Located | undefined {
  let best: Located | undefined;
  for (const s of sightings) if (!best || when(s) > when(best)) best = s;
  return best;
}

/** The most recent sighting in whichever ~2 km cell holds the most of them. */
function busiest(sightings: Located[]): Located | undefined {
  const cells = new Map<string, Located[]>();
  let fullest: Located[] = [];
  for (const s of sightings) {
    const key = `${Math.floor(s.lat / BUSY_CELL_DEG)}:${Math.floor(s.lng / BUSY_CELL_DEG)}`;
    const cell = cells.get(key) ?? [];
    cell.push(s);
    cells.set(key, cell);
    if (cell.length > fullest.length) fullest = cell;
  }
  return latest(fullest);
}

export function homeFrame(sightings: MappableSighting[], here?: Place | null): HomeFrame {
  const located = sightings.filter((s): s is Located => s.lat != null && s.lng != null);
  if (located.length === 0) return { anchor: "none", points: [] };

  const around = (p: Place) => located.filter((s) => metresBetween(p, s) <= NEIGHBOURHOOD_M);
  const pair = (p: Place): [number, number] => [p.lng, p.lat];

  if (here) {
    const nearby = around(here);
    // Only when there is something to look at. An empty stretch of basemap
    // around the office is a worse opening than the dogs you already know.
    if (nearby.length > 0) return { anchor: "here", points: [pair(here), ...nearby.map(pair)] };
  }

  // `mine` is absent on /dex, where every sighting is the viewer's own.
  const own = latest(located.filter((s) => s.mine !== false));
  const anchor = own ?? busiest(located)!;
  return {
    anchor: own ? "mine" : "busiest",
    points: [pair(anchor), ...around(anchor).filter((s) => s !== anchor).map(pair)],
  };
}
