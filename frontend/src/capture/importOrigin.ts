import type { GeoSource } from "../api";

/**
 * Where and when an imported photo says it was taken.
 *
 * A camera-roll photo is not a live capture: using `new Date()` and the current
 * GPS fix for it records it as here-and-now. `captured_at` and `geog` are the
 * two inputs to `resolve_sighting`'s 1km candidate search, so that does not
 * merely mislabel the sighting -- it inserts a phantom into the spatial prior
 * for wherever the phone is standing.
 */
export interface ImportOrigin {
  captured_at: string;
  lat?: number;
  lng?: number;
  geo_accuracy_m?: number;
  geo_source: GeoSource;
}

const NAIVE_LOCAL =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/;

/**
 * Turn EXIF's naive wall-clock time into an absolute instant.
 *
 * `DateTimeOriginal` has no zone. `OffsetTimeOriginal` carries one but most
 * cameras omit it, which is why the server hands both back untouched instead of
 * guessing: assuming UTC would shift an Indian evening capture back five and a
 * half hours, across a day boundary.
 *
 * With no stated offset we interpret the string in the *device's* zone. That is
 * a guess, but a well-founded one -- the photo is on this phone, so it was
 * almost certainly taken by this person, in the zone they were in. It is also
 * the only zone this code can know.
 *
 * Returns null for anything unparseable, so a malformed value falls through to
 * asking rather than inventing a date.
 */
export function resolveCapturedAt(
  local: string | null,
  offsetMinutes: number | null,
): string | null {
  if (!local) return null;
  const m = NAIVE_LOCAL.exec(local.trim());
  if (!m) return null;
  const [, y, mo, d, h, mi, s] = m;
  const year = +y;
  const month = +mo - 1;
  const day = +d;
  const hour = +h;
  const minute = +mi;
  const second = s ? +s : 0;

  const ms =
    offsetMinutes == null
      ? new Date(year, month, day, hour, minute, second).getTime()
      : Date.UTC(year, month, day, hour, minute, second) - offsetMinutes * 60_000;

  if (!Number.isFinite(ms)) return null;
  return new Date(ms).toISOString();
}

/**
 * A photo whose file told us both when and where. No prompt needed.
 */
export function originFromExif(
  capturedAt: string,
  lat: number,
  lng: number,
): ImportOrigin {
  return {
    captured_at: capturedAt,
    lat,
    lng,
    // Not `device_gps` -- that would claim an accuracy we don't have -- and not
    // `pin`, which would claim a human placed it on a map. `geo_accuracy_m`
    // stays absent: EXIF carries no accuracy estimate worth trusting.
    geo_source: "exif",
  };
}

/**
 * A photo the person had to describe, because the file had been stripped.
 * WhatsApp forwards, screenshots and several gallery apps strip EXIF entirely,
 * so this is the common path, not the edge.
 *
 * `pin` rather than `exif` when they supply a location from the device: they are
 * asserting where it happened, which is what a pin means. Omitting the location
 * yields `none` -- the sighting still counts, it just carries no coordinate and
 * so draws no pin.
 */
export function originFromPerson(
  capturedAt: string,
  position: { lat: number; lng: number } | null,
): ImportOrigin {
  if (!position) return { captured_at: capturedAt, geo_source: "none" };
  return {
    captured_at: capturedAt,
    lat: position.lat,
    lng: position.lng,
    geo_source: "pin",
  };
}
