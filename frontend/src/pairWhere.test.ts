import { describe, expect, it } from "vitest";
import type { PairLocation, Proposal } from "./api";
import { cellRing, formatDistance, pairExtent, whenText, whereText } from "./pairWhere";
import { coarsenPlaceBias } from "./precision";

const exact = (lat: number, lng: number): PairLocation => ({ lat, lng, precision: "exact", cell_m: null });
const area = (lat: number, lng: number, cell_m = 1000): PairLocation => ({ lat, lng, precision: "area", cell_m });

function pair(a: PairLocation | null, b: PairLocation | null, distance_m: number | null,
  dates = ["2026-07-01", "2026-07-09"]): Proposal {
  return {
    id: "p", score: 0.5, distance_m,
    a: { sighting_id: "a", date: dates[0], thumb_url: null, location: a },
    b: { sighting_id: "b", date: dates[1], thumb_url: null, location: b },
  };
}

describe("formatDistance", () => {
  it.each([[0, "10 m"], [430, "430 m"], [990, "990 m"], [1000, "1.0 km"], [1830, "1.8 km"], [12_400, "12 km"]])(
    "%d m reads %s", (m, text) => expect(formatDistance(m)).toBe(text));
});

describe("whereText", () => {
  it("is plain when both are exact", () => {
    expect(whereText(pair(exact(1, 1), exact(1, 1.01), 1110))).toEqual({ distance: "1.1 km apart", note: null });
  });
  it("is approximate and says why when one side is an area", () => {
    expect(whereText(pair(exact(1, 1), area(1, 1.02), 2220))).toEqual({
      distance: "About 2.2 km apart",
      note: "Their photo is shown as a ~1 km area, not an exact spot.",
    });
  });
  it("names a shared cell rather than a zero distance", () => {
    expect(whereText(pair(area(1, 1, 2000), area(1, 1, 2000), 0)).distance).toBe("Same ~2 km area");
  });
  it("gives no distance when a side is missing", () => {
    expect(whereText(pair(exact(1, 1), null, null)).distance).toBeNull();
    expect(whereText(pair(null, null, null)).note).toBe("No location to show for either photo.");
  });
});

describe("whenText", () => {
  it("counts the days between, in either order", () => {
    expect(whenText(pair(null, null, null, ["2026-07-09", "2026-07-08"]))).toBe("2026-07-09 and 2026-07-08 · 1 day apart");
    expect(whenText(pair(null, null, null, ["2026-07-09", "2026-07-09"]))).toBe("2026-07-09 and 2026-07-09 · same day");
  });
});

describe("cellRing", () => {
  it("is the very cell the server snapped to", () => {
    const centre = coarsenPlaceBias({ lat: 12.9716, lng: 77.5946 }, 1000)!;
    const ring = cellRing(area(centre.lat, centre.lng))!;
    expect(ring).toHaveLength(5);
    expect(ring[0]).toEqual(ring[4]);
    const [[w, s], , [e, n]] = ring;
    // Points just inside each corner snap back to the same centre.
    for (const [lng, lat] of [[w, s], [e, s], [e, n], [w, n]]) {
      const inside = { lat: lat + (lat < centre.lat ? 1e-5 : -1e-5), lng: lng + (lng < centre.lng ? 1e-5 : -1e-5) };
      expect(coarsenPlaceBias(inside, 1000)).toEqual(centre);
    }
  });
  it("is nothing for an exact point", () => {
    expect(cellRing(exact(1, 1))).toBeNull();
    expect(pairExtent([exact(1, 2)])).toEqual([[2, 1]]);
  });
});
