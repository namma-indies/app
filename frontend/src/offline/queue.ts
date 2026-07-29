// Simple IndexedDB queue for every capture. Every submit enqueues here first
// (see Capture.tsx) -- there is no separate "live" path. flush() drains
// pending items to the server whenever it's called (on enqueue, on app
// open, on the browser's `online` event).
import { openDB, type IDBPDatabase } from "idb";
import { postSighting, UnauthorizedError, type PostSightingInput } from "../api";

const DB_NAME = "indiedex-queue";
const STORE = "pending";

export type QueueStatus = "pending" | "failed";
export type QueuedItem = PostSightingInput & { id: number; status: QueueStatus };

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
  await db.add(STORE, { ...input, status: "pending" as QueueStatus });
}

async function countByStatus(status: QueueStatus): Promise<number> {
  const db = await getDb();
  const all = (await db.getAll(STORE)) as QueuedItem[];
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
  const all = (await db.getAll(STORE)) as QueuedItem[];
  return all.filter((item) => (item.status ?? "pending") === "failed");
}

export async function retryFailed(id: number): Promise<void> {
  const db = await getDb();
  const item = (await db.get(STORE, id)) as QueuedItem | undefined;
  if (!item) return;
  await db.put(STORE, { ...item, status: "pending" satisfies QueueStatus });
}

export async function discardFailed(id: number): Promise<void> {
  const db = await getDb();
  await db.delete(STORE, id);
}

/** A permanent failure means retrying without user action can't succeed:
 * the request was rejected (4xx), not merely unreachable. 401 gets its own
 * branch only for clarity -- it's a `UnauthorizedError` instance, not a
 * generic Error with a status in its message. */
function isPermanentFailure(err: unknown): boolean {
  if (err instanceof UnauthorizedError) return true;
  if (err instanceof Error) {
    const match = /request failed: (\d+)/.exec(err.message);
    if (match) {
      const status = Number(match[1]);
      return status >= 400 && status < 500;
    }
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
      const all = (await db.getAll(STORE)) as QueuedItem[];
      for (const item of all) {
        const status = item.status ?? "pending";
        if (status !== "pending") continue;
        try {
          await postSighting(item);
          await db.delete(STORE, item.id);
        } catch (err) {
          if (isPermanentFailure(err)) {
            await db.put(STORE, { ...item, status: "failed" satisfies QueueStatus });
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
