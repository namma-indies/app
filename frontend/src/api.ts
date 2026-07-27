// Typed fetch wrappers for the IndieDex API. Always send credentials so the
// httpOnly session cookie set by /auth/magic-link/consume is included.

export type GeoSource = "device_gps" | "pin" | "none";
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

export interface PostSightingInput {
  photos: Blob[];
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

async function handle<T>(res: Response): Promise<T> {
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) throw new Error(`request failed: ${res.status}`);
  return (await res.json()) as T;
}

export async function getDex(): Promise<DexResponse> {
  const res = await fetch("/dex", { credentials: "include" });
  return handle<DexResponse>(res);
}

export function buildSightingForm(input: PostSightingInput): FormData {
  const form = new FormData();
  input.photos.forEach((p, i) => form.append("photos", p, `photo-${i}.jpg`));
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
  const res = await fetch("/sighting", {
    method: "POST",
    credentials: "include",
    body: buildSightingForm(input),
  });
  return handle<PostSightingResponse>(res);
}
