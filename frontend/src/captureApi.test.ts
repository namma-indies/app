import { afterEach, describe, expect, it, vi } from "vitest";
import { adaptCapture, getCaptures, multiAnimalIntakeAvailable, postCapture, reviewCapture } from "./captureApi";

afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); });
const ok = (body: unknown) => new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });

describe("capture API boundary", () => {
  it("requires both explicit client opt-in and server capability", async () => {
    const fetch = vi.fn().mockResolvedValue(ok({ multi_animal_enabled: true }));
    vi.stubGlobal("fetch", fetch);
    vi.stubEnv("VITE_MULTI_ANIMAL_ENABLED", undefined);
    expect(await multiAnimalIntakeAvailable()).toBe(false);
    vi.stubEnv("VITE_MULTI_ANIMAL_ENABLED", "false");
    expect(await multiAnimalIntakeAvailable()).toBe(false);
    expect(fetch).not.toHaveBeenCalled();
    vi.stubEnv("VITE_MULTI_ANIMAL_ENABLED", "true");
    expect(await multiAnimalIntakeAvailable()).toBe(true);
    expect(fetch.mock.calls[0][0]).toMatch(/\/me$/);
    fetch.mockResolvedValueOnce(ok({}));
    expect(await multiAnimalIntakeAvailable()).toBe(false);
    fetch.mockRejectedValueOnce(new TypeError("offline"));
    expect(await multiAnimalIntakeAvailable()).toBe(false);
  });
  it.each(["photos", "video"] as const)("uploads %s once, preserving shared metadata and withholding per-animal details", async (media) => {
    const fetch = vi.fn().mockResolvedValue(ok({ capture_id: "c", sighting_ids: [], processing_state: "queued" }));
    vi.stubGlobal("fetch", fetch);
    const blob = new Blob(["media"]);
    await postCapture({ ...(media === "video" ? { video: blob } : { photos: [blob] }), client_token: "durable", geo_source: "exif", lat: 12, lng: 77, captured_at: "2026-09-01T12:00:00Z", reported_at: "2026-09-02T12:00:00Z", note: "shared", sex: "female", condition: "injured", ear_notch: "left" });
    const [url, init] = fetch.mock.calls[0];
    expect(url).toMatch(/\/capture$/);
    expect(init.credentials).toBe("include");
    const body = init.body as FormData;
    expect(body.get(media)).toBeInstanceOf(Blob);
    expect(body.get("client_token")).toBe("durable");
    expect(body.get("geo_source")).toBe("exif");
    expect(body.get("note")).toBe("shared");
    expect(body.get("reported_at")).toBe("2026-09-02T12:00:00Z");
    expect(body.has("sex") || body.has("condition") || body.has("ear_notch")).toBe(false);
  });
  it("reads captures despite a disabled intake flag and adapts the items envelope", async () => {
    vi.stubEnv("VITE_MULTI_ANIMAL_ENABLED", "false");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(ok({ items: [{ capture_id: "pending", processing_state: "processing", sighting_ids: [] }] })));
    expect((await getCaptures()).sightings[0].capture_id).toBe("pending");
  });
  it("sends a complete revision-checked publication review", async () => {
    const fetch = vi.fn().mockResolvedValue(ok({ capture_id: "c", sighting_ids: ["a", "b"], processing_state: "ready" }));
    vi.stubGlobal("fetch", fetch);
    const groups = [{ instance_ids: ["i1"], sex: "female" as const }, { instance_ids: ["i2"], condition: "injured" as const }];
    await reviewCapture("c", groups, 4);
    expect(fetch.mock.calls[0][0]).toMatch(/\/capture\/c\/review$/);
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ groups, revision: 4, publish: true });
  });
  it("trims names and sends explicit null for clearing while preserving missing legacy fields", async () => {
    const fetch = vi.fn().mockResolvedValue(ok({ capture_id: "c", sighting_ids: [], processing_state: "ready" }));
    vi.stubGlobal("fetch", fetch);
    await reviewCapture("c", [{ instance_ids: ["a"], known_name: "  Kaju  " }, { instance_ids: ["b"], known_name: "   " }, { instance_ids: ["c"] }], 2);
    expect(JSON.parse(fetch.mock.calls[0][1].body).groups).toEqual([{ instance_ids: ["a"], known_name: "Kaju" }, { instance_ids: ["b"], known_name: null }, { instance_ids: ["c"] }]);
  });
  it("normalises original-photo boxes independently from crop boxes", () => {
    const capture: Parameters<typeof adaptCapture>[0] = { capture_id: "c", processing_state: "ready", captured_at: "now", note: null, sighting_ids: [], revision: 0, groups: [], instances: [{ instance_id: "i", source_photo_id: "p", track_id: "t", sighting_id: null, species: "dog", bbox: [20, 40, 80, 100], crop_bbox: [10, 20, 110, 120], timestamp_ms: 40, photo_url: "crop.webp", thumb_url: "crop_thumb.webp", source_thumb_url: "source_thumb.webp", source_width: 200, source_height: 400 }] };
    expect(adaptCapture(capture).instances[0]).toMatchObject({ source_thumb_url: "source_thumb.webp", source_width: 200, source_height: 400, source_bbox: [0.1, 0.1, 0.4, 0.25], bbox: [0.1, 0.2, 0.7, 0.8] });
    for (const missing of [{ source_width: undefined }, { source_height: 0 }, { source_width: NaN }, { source_height: Infinity }, { source_thumb_url: undefined }, { source_width: 30 }]) {
      const adapted = adaptCapture({ ...capture, instances: [{ ...capture.instances[0], ...missing }] }).instances[0];
      expect(adapted.source_bbox).toBeUndefined();
      expect(adapted.source_thumb_url).toBeUndefined();
      expect(adapted.bbox).toEqual([0.1, 0.2, 0.7, 0.8]);
    }
  });
  it("normalises raw boxes relative to animal evidence, never the whole scene", () => {
    const detail = adaptCapture({ capture_id: "c", processing_state: "needs_review", captured_at: "now", note: null, sighting_ids: [], revision: 0, groups: [], instances: [{ instance_id: "i", source_photo_id: "p", track_id: "t", sighting_id: null, species: "dog", bbox: [20, 40, 80, 100], crop_bbox: [10, 20, 110, 120], timestamp_ms: 40, photo_url: "crop.webp", thumb_url: "crop_thumb.webp" }] });
    expect(detail.instances[0]).toMatchObject({ id: "i", photo_id: "p", source_url: "crop.webp", bbox: [0.1, 0.2, 0.7, 0.8] });
    expect(detail.instances[0].source_bbox).toBeUndefined();
  });
});
