// Simple IndexedDB queue for every capture. Every submit enqueues here first
// (see Capture.tsx) -- there is no separate "live" path. flush() drains
// pending items to the server whenever it's called (on enqueue, on app
// open, on the browser's `online` event).
import { openDB, type IDBPDatabase } from "idb";
import { HttpError, postSighting, UnauthorizedError, type PostSightingInput } from "../api";

const DB_NAME = "indiedex-queue";
const STORE = "pending";

export type QueueStatus = "pending" | "failed";
export type QueuedItem = PostSightingInput & { id: number; status: QueueStatus };

/** A photo as stored: raw bytes we own outright, plus the MIME type needed to
 * rebuild an equivalent Blob. */
type StoredPhoto = { bytes: ArrayBuffer; type: string };

/** The on-disk record. `photo_data` is the current shape; `photos` is the
 * legacy one, kept readable so captures queued by an older build still sync. */
type StoredItem = Omit<PostSightingInput, "photos"> & {
  id: number;
  status?: QueueStatus;
  photo_data?: StoredPhoto[];
  photos?: Blob[];
};

/** Rebuild the in-memory item a caller expects from whichever shape is on disk.
 *
 * Storing a Blob directly looks like it works and does, everywhere except iOS:
 * a camera File there is a handle to a system temp file, so IndexedDB persists
 * a reference whose target the OS may purge. The Blob then reads as dead, and
 * WebKit responds by serialising the entire FormData to zero bytes -- the
 * request still carries a valid multipart boundary, so the server sees a
 * well-formed body containing nothing and rejects every field as missing.
 * Owning the bytes at capture time is what makes the queue durable.
 */
function toItem(stored: StoredItem): QueuedItem {
  const { photo_data, photos, ...rest } = stored;
  const rebuilt = photo_data
    ? photo_data.map((p) => new Blob([p.bytes], { type: p.type }))
    : (photos ?? []);
  return { ...rest, photos: rebuilt, status: stored.status ?? "pending" } as QueuedItem;
}

let dbPromise: Promise<IDBPDatabase> | null = null;

function getDb() {
  if (!dbPromise) {
    dbPromise = openDB(DB_NAME, 1, {
      upgrade(db) {
        db.createObjectStore(STORE, { keyPath: "id", autoIncrement: true });
      },
    });
  }
  return dbPromise;
}

export async function enqueue(input: PostSightingInput): Promise<void> {
  const db = await getDb();
  const { photos, ...rest } = input;
  // Read the bytes now, while the camera's file is still alive. Deferring this
  // to send time is what stranded every capture on iOS.
  const photo_data: StoredPhoto[] = await Promise.all(
    photos.map(async (p) => ({ bytes: await p.arrayBuffer(), type: p.type || "image/jpeg" })),
  );
  await db.add(STORE, { ...rest, photo_data, status: "pending" as QueueStatus });
}

async function countByStatus(status: QueueStatus): Promise<number> {
  const db = await getDb();
  const all = (await db.getAll(STORE)) as StoredItem[];
  return all.filter((item) => (item.status ?? "pending") === status).length;
}

export function pendingCount(): Promise<number> {
  return countByStatus("pending");
}

export function failedCount(): Promise<number> {
  return countByStatus("failed");
}

export async function listFailed(): Promise<QueuedItem[]> {
  const db = await getDb();
  const all = (await db.getAll(STORE)) as StoredItem[];
  return all.filter((item) => (item.status ?? "pending") === "failed").map(toItem);
}

export async function retryFailed(id: number): Promise<void> {
  const db = await getDb();
  const item = (await db.get(STORE, id)) as StoredItem | undefined;
  if (!item) return;
  await db.put(STORE, { ...item, status: "pending" satisfies QueueStatus });
}

export async function discardFailed(id: number): Promise<void> {
  const db = await getDb();
  await db.delete(STORE, id);
}

/** A permanent failure means retrying without user action can't succeed:
 * the request was rejected (4xx), not merely unreachable. 401 gets its own
 * branch since it's a `UnauthorizedError` instance rather than a `HttpError`.
 * 408 (request timeout) and 429 (rate limited) are 4xx but are meant to be
 * retried -- 429 especially, since a queue drain hammering the server is
 * exactly the case that would trigger it. */
function isPermanentFailure(err: unknown): boolean {
  if (err instanceof UnauthorizedError) return true;
  if (err instanceof HttpError) {
    if (err.status === 408 || err.status === 429) return false;
    return err.status >= 400 && err.status < 500;
  }
  return false;
}

// Notified after every flush() pass finishes, so UI that isn't the caller of
// flush() (e.g. App's badge count) can stay in sync without prop-drilling or
// polling. Kept as a single module-level slot rather than a full event
// emitter -- there's only ever one subscriber (App.tsx).
let onFlushed: (() => void) | null = null;

export function setOnFlushed(cb: (() => void) | null): void {
  onFlushed = cb;
}

// Notified specifically when flush() classifies a failure as a 401 --
// distinct from other permanent (4xx) failures, since a dead session needs
// the invite/login gate, not just a "couldn't sync" badge entry.
let onUnauthorized: (() => void) | null = null;

export function setOnUnauthorized(cb: (() => void) | null): void {
  onUnauthorized = cb;
}

let flushing = false;
let pendingRerun = false;

export async function flush(): Promise<void> {
  // A second call while one is already in progress (e.g. a rapid second
  // capture) must not just no-op -- that would strand its item until the
  // next app-open or `online` event. Instead, request one more full pass
  // once the in-flight one finishes.
  if (flushing) {
    pendingRerun = true;
    return;
  }
  flushing = true;
  try {
    do {
      pendingRerun = false;
      const db = await getDb();
      const all = (await db.getAll(STORE)) as StoredItem[];
      for (const stored of all) {
        const status = stored.status ?? "pending";
        if (status !== "pending") continue;
        const item = toItem(stored);
        try {
          await postSighting(item);
          await db.delete(STORE, stored.id);
        } catch (err) {
          if (isPermanentFailure(err)) {
            // Write back `stored`, not the reconstructed item: persisting the
            // rebuilt Blobs would downgrade the record to the legacy shape.
            await db.put(STORE, { ...stored, status: "failed" satisfies QueueStatus });
            if (err instanceof UnauthorizedError) onUnauthorized?.();
            continue;
          }
          // Retryable (network failure or 5xx): stop here, try the rest later.
          break;
        }
      }
    } while (pendingRerun);
  } finally {
    flushing = false;
    onFlushed?.();
  }
}
