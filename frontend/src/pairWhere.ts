import type { PairLocation, Proposal } from "./api";

/** What a match pair card says about where and when its two sightings were
 * taken. Pure, so the wording is tested without a map.
 *
 * Everything here works from what the server chose to show. Another
 * observer's sighting arrives as its grid-cell centre, and the distance was
 * measured to that centre -- so whenever either side is an area the distance
 * is said as approximate, never as a measurement. */

export function formatDistance(m: number): string {
  if (m < 1000) return `${Math.max(10, Math.round(m / 10) * 10)} m`;
  const km = m / 1000;
  return `${km < 10 ? km.toFixed(1) : Math.round(km)} km`;
}

function wholeDays(a: string, b: string): number | null {
  const ta = Date.parse(`${a}T00:00:00Z`);
  const tb = Date.parse(`${b}T00:00:00Z`);
  if (Number.isNaN(ta) || Number.isNaN(tb)) return null;
  return Math.round(Math.abs(tb - ta) / 86_400_000);
}

export function whenText(p: Proposal): string {
  const days = wholeDays(p.a.date, p.b.date);
  const gap = days === null ? "" : days === 0 ? " · same day" : ` · ${days} day${days === 1 ? "" : "s"} apart`;
  return `${p.a.date} and ${p.b.date}${gap}`;
}

function cellKm(loc: PairLocation): string {
  const km = (loc.cell_m ?? 1000) / 1000;
  return km % 1 === 0 ? km.toFixed(0) : km.toFixed(1);
}

export function whereText(p: Proposal): { distance: string | null; note: string | null } {
  const a = p.a.location ?? null;
  const b = p.b.location ?? null;
  if (!a && !b) return { distance: null, note: "No location to show for either photo." };
  if (!a || !b || p.distance_m == null) {
    return { distance: null, note: "Only one of these photos has a location you can see." };
  }
  const area = [a, b].find((loc) => loc.precision === "area");
  if (!area) return { distance: `${formatDistance(p.distance_m)} apart`, note: null };
  const bothAreas = a.precision === "area" && b.precision === "area";
  const note = bothAreas
    ? `Other people's photos are shown as a ~${cellKm(area)} km area, not an exact spot.`
    : `Their photo is shown as a ~${cellKm(area)} km area, not an exact spot.`;
  if (bothAreas && a.lat === b.lat && a.lng === b.lng) {
    return { distance: `Same ~${cellKm(area)} km area`, note };
  }
  return { distance: `About ${formatDistance(p.distance_m)} apart`, note };
}

/** The square an "area" point stands for, as a closed [lng, lat] ring.
 *
 * The server sends the cell centre; the cell is centre ± half a step on each
 * axis, with the longitude step taken at the centre's latitude -- the same
 * arithmetic as app/precision.py and src/precision.ts. Drawing the square
 * rather than a pin is the point: a pin at the centre asserts the one place
 * in the cell the dog demonstrably is not. */
export function cellRing(loc: PairLocation): [number, number][] | null {
  if (loc.precision !== "area" || !loc.cell_m || loc.cell_m <= 0) return null;
  const latHalf = loc.cell_m / 111_320 / 2;
  const cos = Math.max(Math.cos((loc.lat * Math.PI) / 180), 0.01);
  const lngHalf = loc.cell_m / (111_320 * cos) / 2;
  const w = loc.lng - lngHalf, e = loc.lng + lngHalf, s = loc.lat - latHalf, n = loc.lat + latHalf;
  return [[w, s], [e, s], [e, n], [w, n], [w, s]];
}

/** Every corner the small map has to show: exact points, and whole cells. */
export function pairExtent(locs: PairLocation[]): [number, number][] {
  return locs.flatMap((loc) => cellRing(loc) ?? [[loc.lng, loc.lat] as [number, number]]);
}
