import { afterEach, expect, it, vi } from "vitest";
import { postSighting } from "./api";
import { postCapture } from "./captureApi";

afterEach(() => vi.unstubAllGlobals());

it.each([postSighting, postCapture])("preserves Retry-After on either upload contract", async (send) => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 429, headers: { "Retry-After": "120" } })));
  await expect(send({ photos: [], geo_source: "none", captured_at: "2026-11-03T00:00:00Z" }))
    .rejects.toMatchObject({ status: 429, retryAfterS: 120 });
});
