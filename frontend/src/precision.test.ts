import { describe, expect, it } from "vitest";
import { coarsenPlaceBias } from "./precision";

describe("place-search cells match app/precision.py", () => {
  // Fixed examples calculated with the server's shared-map coarsen().
  it.each([
    [12.345678, 67.891234, 1000, 12.34729, 67.88811],
    [12.345678, 67.891234, 2500, 12.34055, 67.89786],
    [-12.345678, -67.891234, 1000, -12.34729, -67.88811],
    [0, 0, 1000, 0.00449, 0.00449],
  ])("snaps (%s, %s) at %s m", (lat, lng, size, coarseLat, coarseLng) => {
    expect(coarsenPlaceBias({ lat, lng }, size)).toEqual({ lat: coarseLat, lng: coarseLng });
  });

  it("is unchanged when the server snaps the client result again", () => {
    const coarse = coarsenPlaceBias({ lat: 12.345678, lng: 67.891234 }, 1000)!;
    expect(coarsenPlaceBias(coarse, 1000)).toEqual(coarse);
  });

  it("keeps the snapped bias within coordinate bounds at a world edge", () => {
    const coarse = coarsenPlaceBias({ lat: 90, lng: 180 }, 1000)!;
    expect(coarse.lat).toBeLessThanOrEqual(90);
    expect(coarse.lng).toBe(180);
  });
});
