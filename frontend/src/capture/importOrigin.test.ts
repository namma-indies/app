import { describe, expect, it } from "vitest";

import { originFromExif, originFromPerson, resolveCapturedAt } from "./importOrigin";

describe("resolveCapturedAt", () => {
  it("uses a stated EXIF offset rather than the device's zone", () => {
    // 18:42:11 +05:30 is 13:12:11 UTC. This is the case where the file knows
    // its own zone, so no guessing is involved.
    expect(resolveCapturedAt("2026-08-05T18:42:11", 330)).toBe(
      "2026-08-05T13:12:11.000Z",
    );
  });

  it("handles a negative offset", () => {
    expect(resolveCapturedAt("2026-08-05T09:00:00", -240)).toBe(
      "2026-08-05T13:00:00.000Z",
    );
  });

  it("treats a zero offset as UTC, not as 'unstated'", () => {
    expect(resolveCapturedAt("2026-08-05T13:00:00", 0)).toBe(
      "2026-08-05T13:00:00.000Z",
    );
  });

  it("interprets an offsetless timestamp in the device's zone", () => {
    // The only zone this code can know, and a well-founded guess: the photo is
    // on this phone. Asserted against the runtime's own conversion rather than
    // a hardcoded instant, so the test is not TZ-dependent.
    const local = "2026-08-05T18:42:11";
    const expected = new Date(2026, 7, 5, 18, 42, 11).toISOString();
    expect(resolveCapturedAt(local, null)).toBe(expected);
  });

  it("does not silently claim UTC when the offset is unstated", () => {
    // The failure this guards: assuming UTC shifts an Indian evening capture
    // back 5.5 hours, across a day boundary. Only meaningful off-UTC, so it's
    // skipped where the runtime is already at UTC.
    const offsetMin = -new Date(2026, 7, 5).getTimezoneOffset();
    if (offsetMin === 0) return;
    expect(resolveCapturedAt("2026-08-05T18:42:11", null)).not.toBe(
      "2026-08-05T18:42:11.000Z",
    );
  });

  it("accepts a timestamp without seconds, which is what a datetime-local input gives", () => {
    expect(resolveCapturedAt("2026-08-05T18:42", 330)).toBe(
      "2026-08-05T13:12:00.000Z",
    );
  });

  it("returns null for null, so a missing date falls through to asking", () => {
    expect(resolveCapturedAt(null, 330)).toBeNull();
  });

  it("returns null for unparseable input rather than inventing a date", () => {
    for (const bad of ["", "not a date", "2026-08-05", "05/08/2026 18:42"]) {
      expect(resolveCapturedAt(bad, null)).toBeNull();
    }
  });
});

describe("originFromExif", () => {
  it("marks the coordinate as coming from the file", () => {
    const o = originFromExif("2026-08-05T13:12:11.000Z", 12.9352, 77.6245);
    expect(o).toEqual({
      captured_at: "2026-08-05T13:12:11.000Z",
      lat: 12.9352,
      lng: 77.6245,
      geo_source: "exif",
    });
  });

  it("claims no accuracy, because EXIF states none", () => {
    expect(originFromExif("2026-08-05T13:12:11.000Z", 12.9, 77.6).geo_accuracy_m).toBeUndefined();
  });
});

describe("originFromPerson", () => {
  it("is a pin when they supply a location — they're asserting where it happened", () => {
    const o = originFromPerson("2026-08-05T13:12:11.000Z", { lat: 12.9, lng: 77.6 });
    expect(o.geo_source).toBe("pin");
    expect(o.lat).toBe(12.9);
  });

  it("carries no coordinate at all when they skip the location", () => {
    const o = originFromPerson("2026-08-05T13:12:11.000Z", null);
    expect(o).toEqual({ captured_at: "2026-08-05T13:12:11.000Z", geo_source: "none" });
    expect(o.lat).toBeUndefined();
  });
});
