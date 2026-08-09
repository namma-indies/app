import { beforeEach, describe, expect, it, vi } from "vitest";
import "fake-indexeddb/auto";
import { IDBFactory } from "fake-indexeddb";

vi.mock("../api", () => ({
  postSighting: vi.fn(),
  UnauthorizedError: class UnauthorizedError extends Error {},
  HttpError: class HttpError extends Error {
    status: number;
    constructor(status: number) {
      super(`request failed: ${status}`);
      this.status = status;
    }
  },
}));

import { postSighting, UnauthorizedError, HttpError } from "../api";
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
      .mockRejectedValueOnce(new HttpError(422))
      .mockResolvedValueOnce({ sighting_id: "s1", photo_ids: [] });

    await flush();

    expect(await failedCount()).toBe(1);
    expect(await pendingCount()).toBe(0);
  });

  it("treats a 5xx as retryable, not failed", async () => {
    const { enqueue, flush, failedCount, pendingCount } = await freshQueue();
    await enqueue(SAMPLE);
    vi.mocked(postSighting).mockRejectedValue(new HttpError(503));

    await flush();

    expect(await failedCount()).toBe(0);
    expect(await pendingCount()).toBe(1);
  });

  it("treats a 429 as retryable, not failed", async () => {
    const { enqueue, flush, failedCount, pendingCount } = await freshQueue();
    await enqueue(SAMPLE);
    vi.mocked(postSighting).mockRejectedValue(new HttpError(429));

    await flush();

    expect(await failedCount()).toBe(0);
    expect(await pendingCount()).toBe(1);
  });
});

describe("concurrent flush", () => {
  it("doesn't strand an item enqueued during an in-flight flush", async () => {
    const { enqueue, flush, pendingCount } = await freshQueue();
    await enqueue(SAMPLE);

    // Deferred promise so we control exactly when the first item's
    // postSighting call resolves, simulating a slow upload.
    let resolveFirst!: (v: { sighting_id: string; photo_ids: string[] }) => void;
    const firstCall = new Promise<{ sighting_id: string; photo_ids: string[] }>((resolve) => {
      resolveFirst = resolve;
    });

    vi.mocked(postSighting).mockImplementationOnce(() => firstCall);
    vi.mocked(postSighting).mockResolvedValueOnce({ sighting_id: "s2", photo_ids: [] });

    const firstFlush = flush();

    // Simulate a second capture arriving while the first flush is still
    // in-flight: it enqueues a new item and triggers its own flush() call.
    await enqueue(SAMPLE);
    const secondFlush = flush();

    // Let the first upload complete.
    resolveFirst({ sighting_id: "s1", photo_ids: [] });

    await Promise.all([firstFlush, secondFlush]);

    expect(await pendingCount()).toBe(0);
    expect(vi.mocked(postSighting)).toHaveBeenCalledTimes(2);
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

describe("photo storage owns its bytes", () => {
  // iOS backs a camera File with a system temp file. Storing that File in
  // IndexedDB stores a reference, and once the OS purges the original the
  // Blob is dead -- WebKit then serialises the whole FormData to zero bytes
  // rather than erroring, so the server sees a well-formed multipart header
  // with content-length 0 and reports every field missing. Reading the bytes
  // while the file is still alive is the only thing that prevents this.
  it("stores raw bytes rather than the File it was handed", async () => {
    const { enqueue } = await freshQueue();
    const photo = new File([new Uint8Array([1, 2, 3, 4])], "d.jpg", { type: "image/jpeg" });

    await enqueue({ ...SAMPLE, photos: [photo] });

    const idb = await import("idb");
    const raw = await idb.openDB("indiedex-queue", 1);
    const [stored] = await raw.getAll("pending");
    raw.close();

    expect(stored.photos?.[0]).not.toBeInstanceOf(Blob);
    const bytes = stored.photo_data[0].bytes;
    expect(new Uint8Array(bytes)).toEqual(new Uint8Array([1, 2, 3, 4]));
    expect(stored.photo_data[0].type).toBe("image/jpeg");
  });

  it("sends a readable Blob even after the original File is dead", async () => {
    const { enqueue, flush } = await freshQueue();
    const photo = new File([new Uint8Array([9, 8, 7])], "d.jpg", { type: "image/jpeg" });
    await enqueue({ ...SAMPLE, photos: [photo] });

    // The camera's temp file is gone by send time -- reading it now throws,
    // exactly as it does on the device.
    photo.arrayBuffer = () => Promise.reject(new DOMException("not found", "NotFoundError"));

    vi.mocked(postSighting).mockResolvedValue({ sighting_id: "s1", photo_ids: [] });
    await flush();

    const sent = vi.mocked(postSighting).mock.calls[0][0];
    expect(sent.photos).toHaveLength(1);
    const roundTripped = new Uint8Array(await sent.photos![0].arrayBuffer());
    expect(roundTripped).toEqual(new Uint8Array([9, 8, 7]));
  });
});

describe("clip storage owns its bytes too", () => {
  // A clip is the same purgeable camera File handle as a photo, only larger --
  // so more likely to be evicted, not less. enqueue() originally destructured
  // only `photos`, which left the clip stored as a live Blob reference.
  it("stores raw bytes rather than the video File it was handed", async () => {
    const { enqueue } = await freshQueue();
    const clip = new File([new Uint8Array([4, 5, 6])], "c.mp4", { type: "video/mp4" });

    await enqueue({ ...SAMPLE, photos: undefined, video: clip });

    const idb = await import("idb");
    const raw = await idb.openDB("indiedex-queue", 1);
    const [stored] = await raw.getAll("pending");
    raw.close();

    expect(stored.video).toBeUndefined();
    expect(new Uint8Array(stored.video_data.bytes)).toEqual(new Uint8Array([4, 5, 6]));
    expect(stored.video_data.type).toBe("video/mp4");
  });

  // Guards the send path (toItem rebuilding the clip), NOT purge-safety: jsdom's
  // IndexedDB structured-clones a Blob, so a stored live handle survives here
  // even though it would not on a device. The storage test above is the one
  // that fails if enqueue stops owning the bytes.
  it("sends a readable clip even after the original File is dead", async () => {
    const { enqueue, flush } = await freshQueue();
    const clip = new File([new Uint8Array([1, 1, 2, 3])], "c.mp4", { type: "video/mp4" });
    await enqueue({ ...SAMPLE, photos: undefined, video: clip });

    clip.arrayBuffer = () => Promise.reject(new DOMException("not found", "NotFoundError"));

    vi.mocked(postSighting).mockResolvedValue({ sighting_id: "s1", photo_ids: [] });
    await flush();

    const sent = vi.mocked(postSighting).mock.calls[0][0];
    expect(sent.video).toBeDefined();
    const roundTripped = new Uint8Array(await sent.video!.arrayBuffer());
    expect(roundTripped).toEqual(new Uint8Array([1, 1, 2, 3]));
    // A clip sighting must not also claim photos -- the server rejects both.
    expect(sent.photos ?? []).toHaveLength(0);
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
