import { afterEach, describe, expect, it, vi } from "vitest";
import { locate } from "./geolocate";

/**
 * The bug these exist for, in one line: `maximumAge` defaults to 0, which
 * forbids the browser from reusing a fix it already holds.
 *
 * The old call also set `enableHighAccuracy: true` with an 8s timeout, so on a
 * hillside under tree cover every capture waited eight seconds for a cold
 * satellite lock and then saved with no coordinate at all — which drops the
 * sighting off every map, silently, because `/map` filters on
 * `geog IS NOT NULL`. A tester reported it as "kept trying and wouldn't go".
 */

type Success = (p: GeolocationPosition) => void;
type Failure = (e: GeolocationPositionError) => void;

function position(lat = 10.2381, lng = 77.4892, accuracy = 12): GeolocationPosition {
  return {
    coords: { latitude: lat, longitude: lng, accuracy },
    timestamp: Date.now(),
  } as GeolocationPosition;
}

const DENIED = { code: 1, PERMISSION_DENIED: 1 } as GeolocationPositionError;
const UNAVAILABLE = { code: 2, PERMISSION_DENIED: 1 } as GeolocationPositionError;

/** Installs a fake geolocation and records the options of every attempt. */
function install(
  behaviour: (attempt: number, ok: Success, fail: Failure) => void,
): PositionOptions[] {
  const calls: PositionOptions[] = [];
  let n = 0;
  vi.stubGlobal("navigator", {
    userAgent: "test",
    geolocation: {
      getCurrentPosition: (ok: Success, fail: Failure, options?: PositionOptions) => {
        calls.push(options ?? {});
        behaviour(n++, ok, fail);
      },
    },
  });
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe("locate", () => {
  it("asks for a cached fix before forcing a fresh acquisition", async () => {
    // The regression test for the original bug. Whatever else changes, the
    // first attempt must be allowed to reuse a position the browser already
    // has, or every capture pays for a cold lock.
    const calls = install((_n, ok) => ok(position()));

    await locate();

    expect(calls[0].maximumAge).toBeGreaterThan(0);
  });

  it("returns a cached fix immediately without a second attempt", async () => {
    const calls = install((_n, ok) => ok(position()));

    const got = await locate();

    expect(got).toMatchObject({ ok: true, lat: 10.2381, lng: 77.4892, stale: true });
    expect(calls).toHaveLength(1);
  });

  it("falls back to a coarse fix rather than giving up", async () => {
    // The case that produced placeless sightings. A few hundred metres out
    // still draws a pin and still enters the 1 km candidate search; nothing
    // does neither, forever.
    const calls = install((n, ok, fail) => {
      if (n < 2) fail(UNAVAILABLE);
      else ok(position(10.3, 77.5, 850));
    });

    const got = await locate();

    expect(got).toMatchObject({ ok: true, accuracy: 850 });
    expect(calls).toHaveLength(3);
    expect(calls[1].enableHighAccuracy).toBe(true);
    expect(calls[2].enableHighAccuracy).toBe(false);
  });

  it("gives the precise attempt longer than the eight seconds that failed", async () => {
    const calls = install((n, ok, fail) => (n === 0 ? fail(UNAVAILABLE) : ok(position())));

    await locate();

    expect(calls[1].timeout).toBeGreaterThan(8000);
  });

  it("stops immediately when permission was refused", async () => {
    // Retrying a refusal cannot succeed and a browser will not re-prompt, so
    // further attempts only add delay to a wait that is already pointless.
    const calls = install((_n, _ok, fail) => fail(DENIED));

    const got = await locate();

    expect(got).toEqual({ ok: false, reason: "denied" });
    expect(calls).toHaveLength(1);
  });

  it("distinguishes a refusal from a failure to get a fix", async () => {
    // They need completely different words: one is fixable by the person, the
    // other is fixable by walking somewhere else. The old code collapsed both
    // to null and could say neither.
    install((_n, _ok, fail) => fail(UNAVAILABLE));
    expect(await locate()).toEqual({ ok: false, reason: "unavailable" });

    vi.unstubAllGlobals();
    install((_n, _ok, fail) => fail(DENIED));
    expect(await locate()).toEqual({ ok: false, reason: "denied" });
  });

  it("reports an absent geolocation API rather than throwing", async () => {
    // Some webviews expose the property as undefined, where an `in` test
    // passes and the call then throws — which used to reject out of submit()
    // and lose the capture.
    vi.stubGlobal("navigator", { userAgent: "test" });
    expect(await locate()).toEqual({ ok: false, reason: "unsupported" });
  });

  it("carries the accuracy through, and tolerates its absence", async () => {
    install((_n, ok) => ok(position(1, 2, NaN)));
    expect(await locate()).toMatchObject({ ok: true, accuracy: null });
  });
});
