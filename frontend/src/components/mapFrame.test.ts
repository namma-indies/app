import { describe, expect, it } from "vitest";
import { homeFrame, metresBetween, NEIGHBOURHOOD_M } from "./mapFrame";
import type { MappableSighting } from "../api";

let n = 0;
function at(lat: number | null, lng: number | null, extra: Partial<MappableSighting> = {}): MappableSighting {
  n += 1;
  return { id: `s${n}`, lat, lng, captured_at: "2026-10-01T08:00:00Z", attrs: {}, photos: [], ...extra };
}

// Real places, because the bug was real places: one walk in Indiranagar plus
// one sighting on a trip put Bangalore and Goa in the same frame.
const INDIRANAGAR = { lat: 12.9784, lng: 77.6408 };
const GOA = { lat: 15.2993, lng: 74.124 };
const BERKELEY = { lat: 37.8715, lng: -122.273 };
/** About 111 m per 0.001 degrees of latitude. */
const north = (p: { lat: number; lng: number }, metres: number) => ({ lat: p.lat + metres / 111_320, lng: p.lng });

describe("metresBetween", () => {
  it("measures a known distance", () => {
    expect(metresBetween(INDIRANAGAR, north(INDIRANAGAR, 500))).toBeCloseTo(500, -1);
    // Bangalore to Goa is about 450 km as the crow flies.
    expect(metresBetween(INDIRANAGAR, GOA) / 1000).toBeGreaterThan(400);
    expect(metresBetween(INDIRANAGAR, GOA) / 1000).toBeLessThan(500);
  });
});

describe("homeFrame", () => {
  it("has nothing to frame without a located sighting", () => {
    expect(homeFrame([])).toEqual({ anchor: "none", points: [] });
    expect(homeFrame([at(null, null)])).toEqual({ anchor: "none", points: [] });
  });

  it("opens on the latest sighting's neighbourhood, not on everything at once", () => {
    const home = at(INDIRANAGAR.lat, INDIRANAGAR.lng, { captured_at: "2026-11-20T08:00:00Z" });
    const nextDoor = { ...north(INDIRANAGAR, 400) };
    const neighbour = at(nextDoor.lat, nextDoor.lng, { captured_at: "2026-09-01T08:00:00Z" });
    const trip = at(GOA.lat, GOA.lng, { captured_at: "2026-10-10T08:00:00Z" });
    const abroad = at(BERKELEY.lat, BERKELEY.lng, { captured_at: "2026-08-01T08:00:00Z" });

    const frame = homeFrame([abroad, trip, neighbour, home]);

    expect(frame.anchor).toBe("mine");
    expect(frame.points).toEqual([
      [INDIRANAGAR.lng, INDIRANAGAR.lat],
      [nextDoor.lng, nextDoor.lat],
    ]);
  });

  it("follows you: a newer sighting somewhere else moves the frame there", () => {
    const home = at(INDIRANAGAR.lat, INDIRANAGAR.lng, { captured_at: "2026-10-01T08:00:00Z" });
    const trip = at(GOA.lat, GOA.lng, { captured_at: "2026-11-25T08:00:00Z" });
    expect(homeFrame([home, trip]).points).toEqual([[GOA.lng, GOA.lat]]);
  });

  it("leaves out sightings past the edge of the neighbourhood", () => {
    const home = at(INDIRANAGAR.lat, INDIRANAGAR.lng, { captured_at: "2026-11-20T08:00:00Z" });
    const inside = north(INDIRANAGAR, NEIGHBOURHOOD_M - 50);
    const outside = north(INDIRANAGAR, NEIGHBOURHOOD_M + 50);
    const frame = homeFrame([home, at(inside.lat, inside.lng), at(outside.lat, outside.lng)]);
    expect(frame.points).toHaveLength(2);
    expect(frame.points).toContainEqual([inside.lng, inside.lat]);
  });

  it("on the shared map, anchors on your own sightings and shows everyone's around them", () => {
    // Busier elsewhere, and more recent elsewhere -- neither should pull the
    // frame away from the place the viewer actually logs.
    const theirs = Array.from({ length: 6 }, () =>
      at(GOA.lat, GOA.lng, { mine: false, observer: "sam", captured_at: "2026-11-26T08:00:00Z" }));
    const mine = at(INDIRANAGAR.lat, INDIRANAGAR.lng, { mine: true, captured_at: "2026-09-01T08:00:00Z" });
    const nearby = north(INDIRANAGAR, 300);
    const neighbours = at(nearby.lat, nearby.lng, { mine: false, observer: "priya" });

    const frame = homeFrame([...theirs, neighbours, mine]);

    expect(frame.anchor).toBe("mine");
    expect(frame.points).toEqual([
      [INDIRANAGAR.lng, INDIRANAGAR.lat],
      [nearby.lng, nearby.lat],
    ]);
  });

  it("with nothing of your own, opens where the most sightings are", () => {
    const busy = [0, 150, 300].map((m) => {
      const p = north(INDIRANAGAR, m);
      return at(p.lat, p.lng, { mine: false, observer: "priya" });
    });
    const lone = at(BERKELEY.lat, BERKELEY.lng, { mine: false, observer: "sam", captured_at: "2026-11-26T08:00:00Z" });

    const frame = homeFrame([lone, ...busy]);

    expect(frame.anchor).toBe("busiest");
    expect(frame.points).toHaveLength(3);
    expect(frame.points).not.toContainEqual([BERKELEY.lng, BERKELEY.lat]);
  });

  it("opens where you are standing when there are sightings around you", () => {
    const home = at(INDIRANAGAR.lat, INDIRANAGAR.lng, { captured_at: "2026-10-01T08:00:00Z" });
    const trip = at(GOA.lat, GOA.lng, { captured_at: "2026-11-25T08:00:00Z" });
    const here = north(INDIRANAGAR, 200);

    const frame = homeFrame([home, trip], here);

    expect(frame.anchor).toBe("here");
    expect(frame.points).toEqual([
      [here.lng, here.lat],
      [INDIRANAGAR.lng, INDIRANAGAR.lat],
    ]);
  });

  it("ignores where you are standing when nothing has been logged around you", () => {
    // An empty stretch of basemap is a worse opening than the dogs you know.
    const home = at(INDIRANAGAR.lat, INDIRANAGAR.lng);
    const frame = homeFrame([home], BERKELEY);
    expect(frame.anchor).toBe("mine");
    expect(frame.points).toEqual([[INDIRANAGAR.lng, INDIRANAGAR.lat]]);
  });

  it("does not trip over an unreadable date", () => {
    const dated = at(INDIRANAGAR.lat, INDIRANAGAR.lng, { captured_at: "2026-10-01T08:00:00Z" });
    const undated = at(GOA.lat, GOA.lng, { captured_at: "" });
    expect(homeFrame([undated, dated]).points).toEqual([[INDIRANAGAR.lng, INDIRANAGAR.lat]]);
    expect(homeFrame([undated]).points).toEqual([[GOA.lng, GOA.lat]]);
  });
});
