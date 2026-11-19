// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { getDex, HttpError, postSighting, readPhotoMetadata, UnauthorizedError } from "./api";
import { postCapture } from "./captureApi";
import { CONSENT_REQUIRED_EVENT } from "./consentEvents";

const notified = vi.fn();
beforeEach(() => { notified.mockReset(); window.addEventListener(CONSENT_REQUIRED_EVENT, notified); });
afterEach(() => { window.removeEventListener(CONSENT_REQUIRED_EVENT, notified); vi.unstubAllGlobals(); });

it("signals current-policy refusal from ordinary calls, both upload contracts and metadata", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 428, json: async () => ({ detail: "consent_required" }) })));
  const input = { geo_source: "none" as const, captured_at: "2026-11-03T00:00:00Z" };
  for (const request of [() => getDex(), () => postSighting(input), () => postCapture(input),
    () => readPhotoMetadata(new Blob(["photo"]))]) {
    await expect(request()).rejects.toMatchObject({ status: 428 });
  }
  expect(notified).toHaveBeenCalledTimes(4);
});

it("keeps expired-session and restricted-account handling separate", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 401 })));
  await expect(getDex()).rejects.toBeInstanceOf(UnauthorizedError);
  vi.mocked(fetch).mockResolvedValue({ ok: false, status: 403 } as Response);
  await expect(getDex()).rejects.toBeInstanceOf(HttpError);
  expect(notified).not.toHaveBeenCalled();
});
