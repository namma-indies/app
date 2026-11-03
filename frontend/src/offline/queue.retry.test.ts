// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import "fake-indexeddb/auto";
import { IDBFactory } from "fake-indexeddb";

vi.mock("../api", async (original) => ({
  ...(await original<typeof import("../api")>()), postSighting: vi.fn(),
}));
vi.mock("../captureApi", () => ({ postCapture: vi.fn() }));
import { HttpError, postSighting } from "../api";

afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); });

it("keeps bytes and tokens pending across reload, waits, then automatically resumes the whole queue", async () => {
  vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"] });
  vi.setSystemTime(new Date("2026-11-03T12:00:00Z"));
  globalThis.indexedDB = new IDBFactory();
  vi.resetModules();
  const first = await import("./queue");
  await first.enqueue({ photos: [], geo_source: "none", captured_at: "2026-11-03T00:00:00Z" });
  await first.enqueue({ photos: [], geo_source: "none", captured_at: "2026-11-03T00:01:00Z" });
  vi.mocked(postSighting).mockRejectedValueOnce(new HttpError(429, 60));
  await first.flush();
  const token = vi.mocked(postSighting).mock.calls[0][0].client_token;
  expect(await first.pendingCount()).toBe(2);
  expect(await first.failedCount()).toBe(0);
  expect(await first.nextRetryAt()).toBe(Date.now() + 60000);
  await first.flush();
  expect(postSighting).toHaveBeenCalledTimes(1);
  // A real reload discards old JS timers and module state, but not IndexedDB.
  vi.clearAllTimers();
  vi.resetModules();
  const reloaded = await import("./queue");
  vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"] });
  vi.setSystemTime(new Date("2026-11-03T12:00:00Z"));
  expect(Date.now()).toBe(new Date("2026-11-03T12:00:00Z").getTime());
  expect(await reloaded.nextRetryAt()).toBe(Date.now() + 60000);
  await reloaded.flush();
  expect(postSighting).toHaveBeenCalledTimes(1);
  vi.mocked(postSighting).mockResolvedValue({ sighting_id: "accepted", photo_ids: [] });
  await vi.advanceTimersByTimeAsync(60000);
  await vi.waitFor(async () => expect(await reloaded.pendingCount()).toBe(0));
  expect(postSighting).toHaveBeenCalledTimes(3);
  expect(vi.mocked(postSighting).mock.calls[1][0].client_token).toBe(token);
  expect(await reloaded.nextRetryAt()).toBeNull();
});
