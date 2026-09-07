/**
 * Getting a location fix, on a phone, outdoors, where the network is bad.
 *
 * WHAT WENT WRONG BEFORE
 * ---------------------
 * The old call was one `getCurrentPosition` with `enableHighAccuracy: true`, an
 * 8-second timeout, and no `maximumAge`. That last omission is the expensive
 * one: `maximumAge` defaults to 0, which forbids the browser from reusing a fix
 * it already holds and forces a fresh acquisition every single time.
 *
 * In a city that is a slow second. In Kodaikanal — hills, tree cover, indoors —
 * a cold high-accuracy lock routinely takes longer than eight seconds. So the
 * button appeared to hang for eight seconds and then saved the sighting with no
 * location at all, which drops it off every map. A tester reported it as
 * "kept trying and wouldn't go", and they were right.
 *
 * THREE STAGES, CHEAPEST FIRST
 * ---------------------------
 * 1. A cached fix up to two minutes old, returned immediately. You have not
 *    moved far in two minutes and the dog is in front of you; a fix from the
 *    walk up the road is the same place for this purpose.
 * 2. High accuracy, with a real timeout. Worth waiting for when there is no
 *    cache, because `geog` feeds the 1 km candidate search that re-ID runs on.
 * 3. Coarse accuracy. Wi-Fi and cell trilateration answer in about a second and
 *    land within a few hundred metres. That is far better than nothing: nothing
 *    means the sighting never appears on a map at all.
 *
 * WHY THE FAILURE REASON IS CARRIED OUT
 * ------------------------------------
 * The old code collapsed every failure to `null`, so "you denied permission"
 * and "no signal here" were indistinguishable — and a tester who tapped Don't
 * Allow once had no way to find out what had happened or how to undo it. The
 * two need completely different words on screen, so the reason survives.
 */

export type GeoFailure = "denied" | "unavailable" | "unsupported";

export type GeoResult =
  | { ok: true; lat: number; lng: number; accuracy: number | null; stale: boolean }
  | { ok: false; reason: GeoFailure };

/** A fix this old is still this place. Two minutes of walking does not move you
 *  out of the neighbourhood the candidate search cares about. */
const CACHE_MS = 120_000;

/** Long enough for a cold lock under tree cover, short enough that someone
 *  standing in the street does not think the app has frozen. The coarse stage
 *  runs after this, so nobody waits the full time and then gets nothing. */
const PRECISE_TIMEOUT_MS = 12_000;
const COARSE_TIMEOUT_MS = 6_000;

function attempt(options: PositionOptions): Promise<GeolocationPosition | GeoFailure> {
  return new Promise((resolve) => {
    navigator.geolocation.getCurrentPosition(
      (pos) => resolve(pos),
      (err) => {
        // PERMISSION_DENIED is 1. It is the only one worth distinguishing:
        // it is the only one the person can actually fix, and the only one
        // where retrying is pointless.
        resolve(err.code === err.PERMISSION_DENIED ? "denied" : "unavailable");
      },
      options,
    );
  });
}

function toResult(pos: GeolocationPosition, stale: boolean): GeoResult {
  return {
    ok: true,
    lat: pos.coords.latitude,
    lng: pos.coords.longitude,
    accuracy: Number.isFinite(pos.coords.accuracy) ? pos.coords.accuracy : null,
    stale,
  };
}

export async function locate(): Promise<GeoResult> {
  // Checks the value, not just the key: some webviews expose the property as
  // undefined, where an `in` test passes and the call then throws.
  if (typeof navigator === "undefined" || !navigator.geolocation) {
    return { ok: false, reason: "unsupported" };
  }

  // 1. Anything recent the browser already has.
  const cached = await attempt({ maximumAge: CACHE_MS, timeout: 3_000, enableHighAccuracy: false });
  if (typeof cached !== "string") return toResult(cached, true);
  if (cached === "denied") return { ok: false, reason: "denied" };

  // 2. A real fix.
  const precise = await attempt({
    maximumAge: CACHE_MS,
    timeout: PRECISE_TIMEOUT_MS,
    enableHighAccuracy: true,
  });
  if (typeof precise !== "string") return toResult(precise, false);
  if (precise === "denied") return { ok: false, reason: "denied" };

  // 3. Coarse rather than nothing. A sighting a few hundred metres out still
  // draws a pin and still enters the candidate search; one with no coordinate
  // does neither, and is invisible on every map forever.
  const coarse = await attempt({
    maximumAge: CACHE_MS,
    timeout: COARSE_TIMEOUT_MS,
    enableHighAccuracy: false,
  });
  if (typeof coarse !== "string") return toResult(coarse, false);
  return { ok: false, reason: coarse };
}

/**
 * Whether the browser will even show a permission prompt, where it can be asked.
 *
 * The Permissions API is what makes "you turned this off, here is how to turn it
 * back on" possible at all — without it a denied state is indistinguishable
 * from a failed one, and the only honest thing to say is nothing. Not available
 * everywhere, notably older Safari, so absence is reported rather than guessed.
 */
export async function permissionState(): Promise<PermissionState | "unknown"> {
  try {
    if (typeof navigator === "undefined" || !navigator.permissions?.query) return "unknown";
    const status = await navigator.permissions.query({ name: "geolocation" as PermissionName });
    return status.state;
  } catch {
    return "unknown";
  }
}

/**
 * How to undo a refused permission, per platform.
 *
 * Deliberately concrete. "Enable location in your settings" is useless advice
 * on a phone, because the setting is four levels down a menu whose name differs
 * by platform, and a tester who accidentally tapped Don't Allow is stuck. The
 * browser cannot re-prompt once denied, so words are the only repair available.
 */
export function howToReEnable(): string {
  const ua = typeof navigator === "undefined" ? "" : navigator.userAgent;
  if (/iPhone|iPad|iPod/i.test(ua)) {
    return "Settings › Safari › Location, set it to Ask. If IndieDex is on your home screen, it's Settings › IndieDex › Location instead.";
  }
  if (/Android/i.test(ua)) {
    return "Tap the padlock (or ⓘ) next to the address bar › Permissions › Location, and allow it. In the installed app it's Settings › Apps › IndieDex › Permissions › Location.";
  }
  return "Click the padlock next to the address bar, find Location, and set it to Allow. Then reload.";
}
