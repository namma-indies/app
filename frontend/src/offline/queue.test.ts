import { beforeEach, describe, expect, it, vi } from "vitest";
import "fake-indexeddb/auto";
import { IDBFactory } from "fake-indexeddb";

vi.mock("../api", () => ({
  postSighting: vi.fn(),
  UnauthorizedError: class UnauthorizedError extends Error {},
}));

import { postSighting, UnauthorizedError } from "../api";
import type { PostSightingInput } from "../api";

const SAMPLE: PostSightingInput = {
  photos: [],
  geo_source: "none",
  captured_at: "2026-07-19T10:00:00Z",
};

async function freshQueue() {
  // Reset the module's cached DB handle and the fake IndexedDB itself so
  // each test starts from an empty store.
  (globalThis as any).indexedDB = new IDBFactory();
  vi.resetModules();
  return import("./queue");
}

beforeEach(() => {
  vi.mocked(postSighting).mockReset();
});

describe("flush", () => {
  it("leaves a network-failure item pending and stops draining", async () => {
    const { enqueue, flush, pendingCount } = await freshQueue();
    await enqueue(SAMPLE);
    await enqueue(SAMPLE);
    vi.mocked(postSighting).mockRejectedValue(new TypeError("Failed to fetch"));

    await flush();

    expect(await pendingCount()).toBe(2);
  });

  it("marks a 401 as failed and keeps draining the rest", async () => {
    const { enqueue, flush, pendingCount, failedCount } = await freshQueue();
    await enqueue(SAMPLE);
    await enqueue(SAMPLE);
    vi.mocked(postSighting)
      .mockRejectedValueOnce(new UnauthorizedError())
      .mockResolvedValueOnce({ sighting_id: "s1", photo_ids: [] });

    await flush();

    expect(await failedCount()).toBe(1);
    expect(await pendingCount()).toBe(0);
  });

  it("marks a non-401 4xx as failed and keeps draining", async () => {
    const { enqueue, flush, failedCount, pendingCount } = await freshQueue();
    await enqueue(SAMPLE);
    await enqueue(SAMPLE);
    vi.mocked(postSighting)
      .mockRejectedValueOnce(new Error("request failed: 422"))
      .mockResolvedValueOnce({ sighting_id: "s1", photo_ids: [] });

    await flush();

    expect(await failedCount()).toBe(1);
    expect(await pendingCount()).toBe(0);
  });

  it("treats a 5xx as retryable, not failed", async () => {
    const { enqueue, flush, failedCount, pendingCount } = await freshQueue();
    await enqueue(SAMPLE);
    vi.mocked(postSighting).mockRejectedValue(new Error("request failed: 503"));

    await flush();

    expect(await failedCount()).toBe(0);
    expect(await pendingCount()).toBe(1);
  });
});

describe("legacy items with no status field", () => {
  it("still gets picked up and synced by flush()", async () => {
    const { flush, pendingCount } = await freshQueue();

    // Bypass enqueue() to simulate an item written by the pre-status version
    // of the queue: no `status` key at all. Create the DB/store directly via
    // the raw idb API, matching queue.ts's schema, rather than going through
    // enqueue() (which always sets status).
    const idb = await import("idb");
    const raw = await idb.openDB("indiedex-queue", 1, {
      upgrade(db) {
        db.createObjectStore("pending", { keyPath: "id", autoIncrement: true });
      },
    });
    await raw.add("pending", { ...SAMPLE });
    raw.close();

    vi.mocked(postSighting).mockResolvedValue({ sighting_id: "s1", photo_ids: [] });

    await flush();

    expect(await pendingCount()).toBe(0);
    expect(vi.mocked(postSighting)).toHaveBeenCalledTimes(1);
  });
});

describe("retryFailed / discardFailed", () => {
  it("moves a failed item back to pending on retry", async () => {
    const { enqueue, flush, failedCount, pendingCount, listFailed, retryFailed } =
      await freshQueue();
    await enqueue(SAMPLE);
    vi.mocked(postSighting).mockRejectedValue(new UnauthorizedError());
    await flush();
    expect(await failedCount()).toBe(1);

    const [item] = await listFailed();
    await retryFailed(item.id);

    expect(await failedCount()).toBe(0);
    expect(await pendingCount()).toBe(1);
  });

  it("removes a failed item permanently on discard", async () => {
    const { enqueue, flush, failedCount, listFailed, discardFailed } = await freshQueue();
    await enqueue(SAMPLE);
    vi.mocked(postSighting).mockRejectedValue(new UnauthorizedError());
    await flush();
    const [item] = await listFailed();

    await discardFailed(item.id);

    expect(await failedCount()).toBe(0);
  });
});
