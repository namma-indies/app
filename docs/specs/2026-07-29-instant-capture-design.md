# Instant capture, background sync — design spec

**Date:** 2026-07-29
**Status:** Draft for review
**Companion docs:** `docs/specs/2026-07-19-indiedex-mvp-design.md` (MVP scope), `docs/OPERATIONS.md` (dog gate, deploy).

## 1. Problem

Today, tapping LOG IT in Capture blocks on a full network round trip: upload → YOLO dog-detection inference → thumbnail/hash → two S3 puts → DB insert → 201. On a phone on the street, this is several seconds of visible wait before the user gets feedback that the sighting was logged. The app is meant to be used while walking; that wait is friction that discourages logging.

The offline queue (`frontend/src/offline/queue.ts`) already exists, but only as a fallback when the live `postSighting` call fails (network error). The live path and the queued path are two different code routes today.

## 2. What changes

**Unify the two paths.** Every capture always goes through the local queue first. `LOG IT` writes to IndexedDB, shows "Sighting logged 🐾" immediately, and resets the form — regardless of whether the device is online. A background flush then syncs to the server whenever it can. There is no longer a "try live, fall back to queue" branch; there is only the queue, flushed opportunistically.

**Detach dog detection from the sync request.** `POST /sighting` currently runs YOLO inline before responding. Since sync now happens invisibly in the background, this no longer costs the user a foreground wait — but it still costs the *sync request* reliability on a flaky mobile connection (a longer request is a request more likely to fail and stall the queue). YOLO moves to a FastAPI background task that runs after the row is inserted and the response returned.

## 3. Client: capture flow

`Capture.tsx` `submit()`:
1. Build `PostSightingInput` as today.
2. `await enqueue(input)`.
3. Show the toast and reset the form.
4. Call `flush()` **without awaiting it** — sync starts immediately if online, otherwise waits for the next trigger (app open, `online` event).

The existing `try { postSighting(...) } catch { enqueue(...) }` branch in `submit()` is removed. `postSighting` is only ever called from `flush()`.

## 4. Client: queue schema + flush logic

`offline/queue.ts` gains a `status` field on queued items: `'pending' | 'failed'` (default `'pending'`).

`flush()` iterates items with `status: 'pending'` in order and classifies each failure:

- **Retryable** — network failure (fetch throws) or an HTTP 5xx response. Leave `status: 'pending'`, stop draining the queue (mirrors today's behavior: if the network's down, the rest will fail identically, so don't burn time on them).
- **Permanent** — `UnauthorizedError` (401) or any other non-2xx, non-5xx response. Set `status: 'failed'`, continue to the next item — one permanently broken item must not block sync for the rest of the queue.

On success, delete the item as today.

New export: `failedCount(): Promise<number>`, parallel to the existing `pendingCount()`.

## 5. Client: failed-items surfacing

Failures are never pushed live — the user has moved on by the time a permanent failure is known. Instead:

- On app open/foreground, if `failedCount() > 0`, show a small badge: `"N couldn't sync"`.
- Tapping it opens a minimal list of failed items: thumbnail, `captured_at`, and two actions:
  - **Retry** — set `status` back to `'pending'`, call `flush()`.
  - **Discard** — delete the item from the store permanently.

No new "my sightings" list for the happy path — the toast alone remains sufficient feedback for successful captures, per product direction. This failed-items view only appears when there's something to act on.

## 6. Backend: async dog detection

`routes/sighting.py`:
- `create_sighting` stops calling `_max_dog_confidence` before the insert. It stores photos, inserts the sighting row with `dog_confidence = NULL`, and returns 201.
- A FastAPI `BackgroundTasks` task (added as a route parameter) runs after the response: scores each photo with `dog_confidence`, computes the max, and `UPDATE`s the row's `dog_confidence` column.
- Failure handling is unchanged in spirit: `_max_dog_confidence`'s existing fail-open behavior (log and continue, leave `dog_confidence = NULL`) still applies — it just runs after the response instead of before it.

No change to `detect.py`, the dog-gate threshold, or the "label not gate" semantics — only *when* the scoring runs.

## 7. Edge cases

- **Rapid multi-dog captures on one walk:** each `submit()` enqueues instantly and independently; `flush()`'s existing `flushing` guard drains the queue sequentially in the background. No UI-visible effect either way — the user is never waiting on this.
- **Detector failure (bad image, model error):** already fails open (`dog_confidence = NULL`, sighting still saved) — now happens in the background task instead of inline; behavior unchanged, only timing moves.
- **`override_no_dog` field:** still accepted and ignored per the existing back-compat comment in `sighting.py`; unaffected by this change.

## 8. Out of scope

- Any change to the dog-gate threshold or detection model.
- A visible "my sightings" list showing sync state for successful captures (declined — toast is enough).
- Retry backoff/scheduling beyond "on app open, on `online` event, immediately after enqueue" — no exponential backoff, no periodic background timer while the app is foregrounded. If this proves insufficient in the field, revisit.

## 9. Testing

- Frontend: unit tests for `flush()`'s retryable/permanent classification (mock 401, mock 500, mock network throw), and for the `failed → pending` retry transition.
- Backend: test asserting `POST /sighting` returns 201 without `dog_confidence` having been called synchronously (mock/spy on the background task registration), and a second test that the background task correctly updates the row's `dog_confidence` after running.
