import { API_BASE } from "./apiBase";
import { buildSightingForm, HttpError, UnauthorizedError, type PostSightingInput, type SightingAttrs } from "./api";

export type CaptureState = "queued" | "processing" | "needs_review" | "ready" | "no_animal" | "failed" | "legacy";
export interface CaptureSummary {
  capture_id: string;
  processing_state: CaptureState;
  captured_at: string;
  sighting_ids: string[];
  note: string | null;
}
export interface CaptureInstance {
  id: string;
  photo_id: string;
  track_id: string | null;
  sighting_id: string | null;
  species: string;
  thumb_url: string;
  source_url?: string;
  /** Normalised evidence-crop coordinates, not raw source-image pixels. */
  bbox?: [number, number, number, number];
  timestamp_ms?: number | null;
  co_visible_instance_ids?: string[];
}
export interface CaptureGroup {
  instance_ids: string[];
  sex?: SightingAttrs["sex"] | null;
  ear_notch?: SightingAttrs["ear_notch"] | null;
  condition?: SightingAttrs["condition"] | null;
}
export interface CaptureDetail extends CaptureSummary {
  revision: number;
  instances: CaptureInstance[];
  groups: CaptureGroup[];
}
export interface PostCaptureResponse {
  capture_id: string;
  processing_state: CaptureState;
  sighting_ids: string[];
  duplicate?: boolean;
}

/** Kept at the boundary: backend capture_contracts.InstanceEvidence uses raw pixels. */
interface WireInstance {
  instance_id: string;
  source_photo_id: string;
  track_id: string;
  sighting_id: string | null;
  species: string;
  bbox: [number, number, number, number];
  crop_bbox: [number, number, number, number];
  timestamp_ms: number | null;
  photo_url: string;
  thumb_url: string;
}
interface WireCapture extends CaptureSummary {
  revision: number;
  instances: WireInstance[];
  groups: CaptureGroup[];
}
export function adaptCapture(wire: WireCapture): CaptureDetail {
  return { ...wire, instances: wire.instances.map((instance) => {
    const [x, y, right, bottom] = instance.crop_bbox;
    const [x1, y1, x2, y2] = instance.bbox;
    return {
      id: instance.instance_id, photo_id: instance.source_photo_id,
      track_id: instance.track_id, sighting_id: instance.sighting_id,
      species: instance.species, thumb_url: instance.thumb_url,
      source_url: instance.photo_url, timestamp_ms: instance.timestamp_ms,
      bbox: [(x1 - x) / (right - x), (y1 - y) / (bottom - y), (x2 - x) / (right - x), (y2 - y) / (bottom - y)],
    };
  }) };
}
async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { ...init, credentials: "include" });
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) throw new HttpError(res.status);
  return res.json() as Promise<T>;
}

export async function multiAnimalIntakeAvailable(signal?: AbortSignal): Promise<boolean> {
  if (import.meta.env.VITE_MULTI_ANIMAL_ENABLED !== "true") return false;
  try {
    const result = await request<{ multi_animal_enabled?: boolean }>("/me", { signal });
    return result.multi_animal_enabled === true;
  } catch {
    // Never silently switch a saved capture to the legacy upload contract.
    return false;
  }
}
export function postCapture(input: PostSightingInput): Promise<PostCaptureResponse> {
  const { sex: _sex, ear_notch: _ear, condition: _condition, ...shared } = input;
  return request("/capture", { method: "POST", body: buildSightingForm(shared) });
}
export async function getCaptures(signal?: AbortSignal): Promise<{ sightings: CaptureSummary[] }> {
  try {
    const result = await request<{ items: WireCapture[] }>("/captures", { signal });
    return { sightings: result.items };
  } catch (err) {
    // Readers stay available after intake is disabled; old servers have no route.
    if (err instanceof HttpError && err.status === 404) return { sightings: [] };
    throw err;
  }
}
export async function getCapture(id: string, signal?: AbortSignal): Promise<CaptureDetail> {
  return adaptCapture(await request<WireCapture>(`/capture/${encodeURIComponent(id)}`, { signal }));
}
export function reviewCapture(id: string, groups: CaptureGroup[], revision: number): Promise<PostCaptureResponse> {
  return request(`/capture/${encodeURIComponent(id)}/review`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ groups, revision, publish: true }),
  });
}
