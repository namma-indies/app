// Typed fetch wrappers for the IndieDex API. Always send credentials so the
// httpOnly session cookie set by /auth/magic-link/consume is included.

import { API_BASE } from "./apiBase";

// "exif" is a camera-roll import: coordinates read out of the file rather than
// observed live. It exists as its own value because "device_gps" would claim an
// accuracy we don't have and "pin" would claim a human placed it on a map --
// and `resolve_sighting` filters candidates on a 1km radius, so where a
// sighting claims to be is load-bearing for re-identification.
export type GeoSource = "device_gps" | "pin" | "none" | "exif";
export type Sex = "male" | "female" | "unsure";
export type EarNotch = "none" | "left" | "right" | "unsure";
export type Condition = "healthy" | "injured" | "unsure";

export interface Photo {
  url: string;
  thumb_url: string;
}

export interface SightingAttrs {
  note?: string;
  sex?: Sex;
  ear_notch?: EarNotch;
  condition?: Condition;
}

export interface Sighting {
  id: string;
  captured_at: string;
  lat: number | null;
  lng: number | null;
  geo_accuracy_m: number | null;
  attrs: SightingAttrs;
  photos: Photo[];
}

export interface DexResponse {
  sightings: Sighting[];
}

/** The subset of a sighting the map actually renders. `/dex` and `/map` return
 * different payloads -- the grid needs full-resolution originals, the map needs
 * thumbnails and nothing else -- so `DogMap` is typed on what it reads rather
 * than on either response. */
export interface MappableSighting {
  id: string;
  captured_at: string;
  lat: number | null;
  lng: number | null;
  attrs: SightingAttrs;
  photos: { thumb_url: string }[];
  /** Absent on /dex responses, where every sighting is the viewer's. */
  observer?: string;
  mine?: boolean;
}

export interface MapSighting extends MappableSighting {
  geo_accuracy_m: number | null;
  observer: string;
  mine: boolean;
}

export interface MapResponse {
  sightings: MapSighting[];
}

/** Everyone's sightings, not just the viewer's. One request serves both sides
 * of the Mine/Everyone toggle: each sighting carries `mine`, so the toggle is a
 * filter over what we already have rather than another round trip. */
export async function getMap(): Promise<MapResponse> {
  const res = await fetch(`${API_BASE}/map`, { credentials: "include" });
  return handle<MapResponse>(res);
}

export interface PhotoMetadata {
  captured_at_local: string | null;
  utc_offset_minutes: number | null;
  lat: number | null;
  lng: number | null;
  has_date: boolean;
  has_location: boolean;
}

export const NO_METADATA: PhotoMetadata = {
  captured_at_local: null,
  utc_offset_minutes: null,
  lat: null,
  lng: null,
  has_date: false,
  has_location: false,
};

/** Bytes of the file to send for the EXIF preflight. EXIF's APP1 segment is
 * capped at 64KB by the spec and sits at the front of the file, so this always
 * contains it -- and the preflight costs a slice instead of a second upload of
 * a 4MB photo. Kept in sync with MAX_HEAD_BYTES in
 * backend/app/routes/photo_metadata.py. */
export const EXIF_HEAD_BYTES = 131_072;

/** Ask the server what a camera-roll photo already knows about itself.
 *
 * Resolves to NO_METADATA rather than rejecting when the call fails: the
 * IndexedDB queue exists because the network is unreliable on the street, and a
 * failed preflight should fall through to asking the person, not strand them
 * mid-import. A 401 is the exception -- an expired session needs surfacing, or
 * the user picks photos into a void.
 */
export async function readPhotoMetadata(file: Blob): Promise<PhotoMetadata> {
  const form = new FormData();
  form.append("head", file.slice(0, EXIF_HEAD_BYTES), "head.jpg");
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/photo/metadata`, {
      method: "POST",
      credentials: "include",
      body: form,
    });
  } catch {
    return NO_METADATA;
  }
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) return NO_METADATA;
  try {
    return (await res.json()) as PhotoMetadata;
  } catch {
    return NO_METADATA;
  }
}

export interface PostSightingInput {
  photos?: Blob[];
  video?: Blob;
  lat?: number;
  lng?: number;
  geo_accuracy_m?: number;
  geo_source: GeoSource;
  captured_at: string;
  reported_at?: string;
  note?: string;
  sex?: Sex;
  ear_notch?: EarNotch;
  condition?: Condition;
}

export interface PostSightingResponse {
  sighting_id: string;
  photo_ids: string[];
}

export class UnauthorizedError extends Error {
  constructor() {
    super("unauthorized");
  }
}

// Carries the HTTP status as a real field rather than embedding it in the
// message string, so callers (queue.ts's failure classification) don't have
// to regex-parse prose that could change independently of this file.
export class HttpError extends Error {
  constructor(public status: number) {
    super(`request failed: ${status}`);
  }
}

async function handle<T>(res: Response): Promise<T> {
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) throw new HttpError(res.status);
  return (await res.json()) as T;
}

export async function getDex(): Promise<DexResponse> {
  const res = await fetch(`${API_BASE}/dex`, { credentials: "include" });
  return handle<DexResponse>(res);
}

export function buildSightingForm(input: PostSightingInput): FormData {
  const form = new FormData();
  if (input.video) {
    form.append("video", input.video, "clip.mp4");
  } else {
    (input.photos ?? []).forEach((p, i) => form.append("photos", p, `photo-${i}.jpg`));
  }
  if (input.lat !== undefined) form.append("lat", String(input.lat));
  if (input.lng !== undefined) form.append("lng", String(input.lng));
  if (input.geo_accuracy_m !== undefined)
    form.append("geo_accuracy_m", String(input.geo_accuracy_m));
  form.append("geo_source", input.geo_source);
  form.append("captured_at", input.captured_at);
  if (input.reported_at) form.append("reported_at", input.reported_at);
  if (input.note) form.append("note", input.note);
  if (input.sex) form.append("sex", input.sex);
  if (input.ear_notch) form.append("ear_notch", input.ear_notch);
  if (input.condition) form.append("condition", input.condition);
  return form;
}

export async function postSighting(
  input: PostSightingInput,
): Promise<PostSightingResponse> {
  const res = await fetch(`${API_BASE}/sighting`, {
    method: "POST",
    credentials: "include",
    body: buildSightingForm(input),
  });
  return handle<PostSightingResponse>(res);
}
