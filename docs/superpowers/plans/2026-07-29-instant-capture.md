# Instant Capture, Background Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capturing a sighting never waits on the network — `LOG IT` writes locally and confirms instantly, and dog-detection scoring moves off the server's sync-request path so background syncs stay short and reliable.

**Architecture:** Client always enqueues to the existing IndexedDB queue (`offline/queue.ts`) before showing success, then kicks a background `flush()`. `flush()` gains failure classification (retryable vs. permanent) so one broken item can't block the queue, and a `failed` status surfaces via a small badge with retry/discard actions. Server-side, `POST /sighting` inserts with `dog_confidence = NULL` and returns immediately; a FastAPI `BackgroundTasks` job scores the photo(s) and updates the row afterward.

**Tech Stack:** FastAPI + asyncpg + Starlette `BackgroundTasks` (backend, `app/backend`), React + TypeScript + Vite + `idb` (frontend, `app/frontend`). Frontend testing is currently unconfigured — this plan introduces Vitest + `fake-indexeddb` as dev dependencies.

## Global Constraints

- Dog detection remains a **label, never a gate** — every sighting saves regardless of score (per `docs/specs/2026-07-29-instant-capture-design.md` §6, unchanged from existing `detect.py`/`sighting.py` semantics).
- No new "my sightings" sync-state list for successful captures — the toast alone is the happy-path feedback (design §5).
- No retry backoff/scheduling beyond: on app open, on `online` event, and immediately after enqueue (design §8).
- `override_no_dog` form field stays accepted-and-ignored (existing back-compat comment in `sighting.py`) — do not remove it.

---

## Task 1: Backend — detach dog detection into a background task

**Files:**
- Modify: `app/backend/app/routes/sighting.py`
- Test: `app/backend/tests/test_sighting.py`

**Interfaces:**
- Consumes: `app.detect.dog_confidence`, `app.detect.DOG_CONF_THRESHOLD` (existing, unchanged).
- Produces: `_score_and_save_dog_confidence(pool: asyncpg.Pool, sighting_id: UUID, raws: list[bytes]) -> None` — new module-level function other tasks don't need to call directly, but its name/signature is referenced by this task's own tests.

Currently `create_sighting` (lines 38-182) calls `dog_conf = await _max_dog_confidence(raws)` at line 69, **before** the DB insert, and passes `dog_conf` as a bind parameter into both INSERT branches (lines 122/138 and 146/159). This blocks the response on YOLO inference. Move it to run after the response is prepared, via `BackgroundTasks`.

- [ ] **Step 1: Write the failing tests**

Add to `app/backend/tests/test_sighting.py` (these exercise the new background-scoring path; the existing `test_post_sighting_saves_when_no_dog_detected` and `test_post_sighting_records_dog_confidence` already assert the *end state* of `dog_confidence` and continue to pass unchanged, since Starlette runs `BackgroundTasks` before an in-process ASGI test call returns):

```python
@pytest.mark.asyncio
async def test_post_sighting_dog_confidence_null_until_background_task_runs(
    authed_client, monkeypatch
):
    """The insert itself must not depend on the detector: even if scoring is
    slow or fails, the row exists with dog_confidence NULL until the
    background task updates it."""
    from app.routes import sighting as sighting_route

    def boom(_raw):
        raise RuntimeError("simulated detector failure")

    # sighting.py does `from app.detect import dog_confidence`, which binds
    # its own name in this module's namespace -- patch that name, not
    # app.detect's, or the patch has no effect on the code under test.
    monkeypatch.setattr(sighting_route, "dog_confidence", boom)

    client, _ = authed_client
    r = await client.post(
        "/sighting",
        files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
        data={"geo_source": "none", "captured_at": "2026-07-19T10:00:00Z"},
    )
    assert r.status_code == 201
    sid = r.json()["sighting_id"]
    pool = client._transport.app.state.pool
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT dog_confidence, review_status FROM sightings WHERE id=$1", sid
        )
    # Detector failure fails open: sighting still saved, just unscored.
    assert row["dog_confidence"] is None
    assert row["review_status"] == "valid"


@pytest.mark.asyncio
async def test_post_sighting_insert_does_not_call_detector_directly(
    authed_client, monkeypatch
):
    """Regression guard for the whole point of this change: the detector
    must be invoked from the background task, not inline in the request
    handler, so a call inside `create_sighting`'s own body (before the
    response is built) never happens."""
    from app.routes import sighting as sighting_route

    calls = []
    orig = sighting_route._score_and_save_dog_confidence

    async def spy(pool, sighting_id, raws):
        calls.append(sighting_id)
        await orig(pool, sighting_id, raws)

    monkeypatch.setattr(sighting_route, "_score_and_save_dog_confidence", spy)

    client, _ = authed_client
    r = await client.post(
        "/sighting",
        files={"photos": ("d.jpg", _jpeg(), "image/jpeg")},
        data={"geo_source": "none", "captured_at": "2026-07-19T10:00:00Z"},
    )
    assert r.status_code == 201
    sid = r.json()["sighting_id"]
    assert calls == [UUID(sid)]
```

Add `from uuid import UUID` to the top of `test_sighting.py` alongside the existing imports.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd app/backend && uv run pytest tests/test_sighting.py -k "background_task or null_until" -v`
Expected: FAIL — `_score_and_save_dog_confidence` does not exist yet (`AttributeError` / `ImportError`), and the existing insert still computes `dog_conf` inline.

- [ ] **Step 3: Implement the background task**

In `app/backend/app/routes/sighting.py`:

Add imports: `from fastapi import BackgroundTasks` (alongside the existing `fastapi` import on line 7) and `import asyncpg` (for the type hint).

Add a new function after `_max_dog_confidence` (after line 35):

```python
async def _score_and_save_dog_confidence(
    pool: asyncpg.Pool, sighting_id: UUID, raws: list[bytes]
) -> None:
    """Runs after the sighting is already saved. Scoring is a label, never a
    gate, so a failure here (bad image, model error) just leaves
    dog_confidence NULL -- it must never affect whether the sighting exists."""
    dog_conf = await _max_dog_confidence(raws)
    if dog_conf is None:
        return
    if dog_conf < DOG_CONF_THRESHOLD:
        logger.info(
            "low dog confidence, saved anyway: conf=%.3f threshold=%.2f sighting=%s",
            dog_conf,
            DOG_CONF_THRESHOLD,
            sighting_id,
        )
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE sightings SET dog_confidence=$1 WHERE id=$2", dog_conf, sighting_id
        )
```

Add `from uuid import UUID` is already imported (line 5) — reuse it as the type hint above.

In `create_sighting` (starting line 38):
- Add `background_tasks: BackgroundTasks` and `request: Request` as parameters (add `from fastapi import Request` to the existing `fastapi` import line). Every existing parameter in this signature has a default (`File(...)`, `Form(...)`, `Depends(...)` all count as defaults), and Python requires non-default parameters to come before defaulted ones — so add these two as the **first two parameters**, before `photos: list[UploadFile] = File(...)`:

```python
async def create_sighting(
    background_tasks: BackgroundTasks,
    request: Request,
    photos: list[UploadFile] = File(...),
    ...
```

(keep the rest of the existing parameter list unchanged after `photos`)
- Delete the whole block at lines 64-78 (`# Dog detection is a LABEL...` comment through the `logger.info(...)` call) — scoring no longer happens here.
- Replace every `dog_conf` bind parameter in the two INSERT statements (the `$11`/`$9` positions at lines 122-138 and 146-159, and the trailing `dog_conf,` argument at line 138 and line 159) with `None` — the row is inserted with `dog_confidence = NULL`.
- After the `async with conn.transaction():` block finishes (i.e., after line 174, before the `return JSONResponse(...)` at line 176), add:

```python
    background_tasks.add_task(
        _score_and_save_dog_confidence, request.app.state.pool, sighting_id, raws
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd app/backend && uv run pytest tests/test_sighting.py -v`
Expected: PASS — all existing tests plus the two new ones.

- [ ] **Step 5: Commit**

```bash
cd app/backend
git add app/routes/sighting.py tests/test_sighting.py
git commit -m "Move dog-detection scoring off the sighting-save request path"
```

---

## Task 2: Frontend — queue schema gains pending/failed status + flush classification

**Files:**
- Modify: `frontend/src/offline/queue.ts`
- Create: `frontend/src/offline/queue.test.ts`
- Modify: `frontend/package.json` (add Vitest + `fake-indexeddb` as dev deps and a `test` script)
- Create: `frontend/vitest.config.ts`

**Interfaces:**
- Consumes: `postSighting` and `UnauthorizedError` from `../api` (existing).
- Produces: `enqueue(input): Promise<void>` (existing, unchanged signature), `pendingCount(): Promise<number>` (existing, unchanged), `failedCount(): Promise<number>` (new), `flush(): Promise<void>` (existing signature, new internal behavior), `retryFailed(id: number): Promise<void>` (new), `discardFailed(id: number): Promise<void>` (new), `listFailed(): Promise<QueuedItem[]>` (new) — `QueuedItem` is `PostSightingInput & { id: number; status: "pending" | "failed" }`.

- [ ] **Step 1: Add test tooling**

Install dev dependencies:

```bash
cd app/frontend
npm install -D vitest fake-indexeddb
```

Add to `package.json` `scripts`: `"test": "vitest run"`.

Create `frontend/vitest.config.ts`:

```typescript
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    setupFiles: ["fake-indexeddb/auto"],
  },
});
```

- [ ] **Step 2: Write the failing tests**

Create `frontend/src/offline/queue.test.ts`:

```typescript
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd app/frontend && npm test`
Expected: FAIL — `failedCount`, `retryFailed`, `discardFailed`, `listFailed` are not exported yet, and `flush()` doesn't classify failures.

- [ ] **Step 4: Implement the classification logic**

Rewrite `frontend/src/offline/queue.ts`:

```typescript
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
  return all.filter((item) => item.status === status).length;
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
  return all.filter((item) => item.status === "failed");
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

let flushing = false;

export async function flush(): Promise<void> {
  if (flushing) return;
  flushing = true;
  try {
    const db = await getDb();
    const all = (await db.getAll(STORE)) as QueuedItem[];
    for (const item of all) {
      if (item.status !== "pending") continue;
      try {
        await postSighting(item);
        await db.delete(STORE, item.id);
      } catch (err) {
        if (isPermanentFailure(err)) {
          await db.put(STORE, { ...item, status: "failed" satisfies QueueStatus });
          continue;
        }
        // Retryable (network failure or 5xx): stop here, try the rest later.
        break;
      }
    }
  } finally {
    flushing = false;
  }
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd app/frontend && npm test`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
cd app/frontend
git add src/offline/queue.ts src/offline/queue.test.ts package.json package-lock.json vitest.config.ts
git commit -m "Classify queue failures as retryable vs permanent"
```

---

## Task 3: Frontend — Capture.tsx always enqueues first, never awaits the network

**Files:**
- Modify: `frontend/src/screens/Capture.tsx`

**Interfaces:**
- Consumes: `enqueue`, `flush` from `./offline/queue` (Task 2).

- [ ] **Step 1: Update `submit()`**

In `frontend/src/screens/Capture.tsx`, replace the `submit` function (lines 103-143) with:

```typescript
  async function submit() {
    if (!photo) return;
    setSubmitting(true);
    const capturedAt = new Date().toISOString();
    const position = await getLocation();

    const geoSource: GeoSource = position ? "device_gps" : "none";
    const input = {
      photos: [photo],
      lat: position?.coords.latitude,
      lng: position?.coords.longitude,
      geo_accuracy_m: position?.coords.accuracy,
      geo_source: geoSource,
      captured_at: capturedAt,
      note: note || undefined,
      sex: sex || undefined,
      ear_notch: earNotch || undefined,
      condition: condition || undefined,
    };

    try {
      await enqueue(input);
      showToast("Sighting logged 🐾");
      reset();
      flush();
    } catch {
      showToast("Couldn't save. Try again.");
    } finally {
      setSubmitting(false);
    }
  }
```

Remove the now-unused `postSighting` and `UnauthorizedError` imports from the top of the file (lines 2-9) — replace:

```typescript
import {
  postSighting,
  UnauthorizedError,
  type Condition,
  type EarNotch,
  type GeoSource,
  type Sex,
} from "../api";
```

with:

```typescript
import { type Condition, type EarNotch, type GeoSource, type Sex } from "../api";
```

Also update the existing `offline/queue` import on line 10 from:

```typescript
import { enqueue } from "../offline/queue";
```

to:

```typescript
import { enqueue, flush } from "../offline/queue";
```

`onUnauthorized` remains a prop on `Capture` (still used elsewhere via `App.tsx`'s auth probe) but is no longer invoked from `submit()` — enqueue/flush never surfaces auth state synchronously to this screen. Leave the prop and its type signature in place; only its use inside `submit()` is removed.

- [ ] **Step 2: Typecheck and manually verify**

Run: `cd app/frontend && npm run typecheck`
Expected: no errors (confirms `onUnauthorized` prop type still lines up even though `submit()` no longer references it, and no dangling imports).

Run: `cd app/frontend && npm run dev`, open the app, take a photo, tap LOG IT. Expected: the toast appears and the form resets immediately, with no visible delay — confirm in the browser Network tab that the `POST /sighting` request fires but the UI does not wait on it.

- [ ] **Step 3: Commit**

```bash
cd app/frontend
git add src/screens/Capture.tsx
git commit -m "Capture always enqueues locally first; never waits on the network"
```

---

## Task 4: Frontend — failed-sync badge and retry/discard view

**Files:**
- Create: `frontend/src/components/FailedSightings.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: `failedCount`, `listFailed`, `retryFailed`, `discardFailed`, `flush` from `../offline/queue` (Task 2), `QueuedItem` type (Task 2).
- Produces: `FailedSightings` component — `export default function FailedSightings({ onClose }: { onClose: () => void }): JSX.Element`, rendered as an overlay (mirrors the existing `.viewer-overlay` pattern in `Dex.tsx`).

- [ ] **Step 1: Create the component**

Create `frontend/src/components/FailedSightings.tsx`:

```typescript
import { useEffect, useState } from "react";
import { discardFailed, flush, listFailed, retryFailed, type QueuedItem } from "../offline/queue";

export default function FailedSightings({ onClose }: { onClose: () => void }) {
  const [items, setItems] = useState<QueuedItem[]>([]);

  useEffect(() => {
    listFailed().then(setItems);
  }, []);

  async function refresh() {
    setItems(await listFailed());
  }

  async function retry(id: number) {
    await retryFailed(id);
    await refresh();
    flush();
  }

  async function discard(id: number) {
    await discardFailed(id);
    await refresh();
  }

  return (
    <div className="viewer-overlay" onClick={onClose}>
      <div className="failed-sheet" onClick={(e) => e.stopPropagation()}>
        <h2>Couldn't sync</h2>
        {items.length === 0 ? (
          <p className="hint">All caught up.</p>
        ) : (
          <ul className="failed-list">
            {items.map((item) => (
              <li key={item.id}>
                <span className="failed-when">
                  {new Date(item.captured_at).toLocaleString()}
                </span>
                <div className="failed-actions">
                  <button className="btn btn-secondary" onClick={() => discard(item.id)}>
                    DISCARD
                  </button>
                  <button className="btn btn-primary" onClick={() => retry(item.id)}>
                    RETRY
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
        <button className="btn btn-secondary" onClick={onClose}>
          CLOSE
        </button>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Wire the badge into `App.tsx`**

In `frontend/src/App.tsx`:

Add to imports: `import { failedCount, flush } from "./offline/queue";` (replacing the existing `import { flush } from "./offline/queue";` on line 4) and `import FailedSightings from "./components/FailedSightings";`.

Add state and an effect, near the existing `unauthorized`/`checkingAuth` state (after line 12):

```typescript
  const [failed, setFailed] = useState(0);
  const [showFailed, setShowFailed] = useState(false);
```

Extend the existing `flush`/`online` effect (lines 14-18) to also refresh the failed count once flush settles:

```typescript
  useEffect(() => {
    async function run() {
      await flush();
      setFailed(await failedCount());
    }
    run();
    window.addEventListener("online", run);
    return () => window.removeEventListener("online", run);
  }, []);
```

In the topbar JSX (inside the `<div className="topbar">` block, after the `<span className="brand">...</span>`), add:

```typescript
        {failed > 0 && (
          <button className="failed-badge" onClick={() => setShowFailed(true)}>
            {failed} couldn't sync
          </button>
        )}
```

At the end of the component's returned JSX, just before the closing `</div>` of the outer `.app` div, add:

```typescript
      {showFailed && (
        <FailedSightings
          onClose={() => {
            setShowFailed(false);
            failedCount().then(setFailed);
          }}
        />
      )}
```

- [ ] **Step 3: Add styles**

Append to `frontend/src/styles.css`:

```css
/* --- Failed-sync badge + sheet --- */
.failed-badge {
  margin-left: auto; border: 2px solid var(--danger); background: var(--panel);
  color: var(--danger); border-radius: 4px; padding: 0.3rem 0.55rem;
  font-family: "Silkscreen", monospace; font-size: 0.6rem; cursor: pointer;
}

.failed-sheet {
  background: var(--panel); border: 3px solid var(--line); border-radius: 6px;
  padding: 1rem; max-width: 90vw; width: 22rem; max-height: 70vh;
  overflow-y: auto; display: flex; flex-direction: column; gap: 0.7rem;
}
.failed-sheet h2 { margin: 0; color: var(--accent); font-size: 0.95rem; }
.failed-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.6rem; }
.failed-list li {
  display: flex; align-items: center; justify-content: space-between; gap: 0.5rem;
  border: 2px solid var(--line); border-radius: 4px; padding: 0.5rem 0.6rem;
}
.failed-when { font-size: 0.75rem; color: var(--muted); }
.failed-actions { display: flex; gap: 0.4rem; }
```

- [ ] **Step 4: Manual verification**

Run: `cd app/frontend && npm run typecheck && npm run build`
Expected: no errors.

Run `npm run dev`, and in the browser console call the queue functions directly to simulate a permanent failure (e.g. temporarily point `postSighting` at a bad URL, or use the devtools IndexedDB panel to add an item with `status: "failed"` to the `indiedex-queue` DB's `pending` store), reload, and confirm the badge appears with the correct count, and that RETRY / DISCARD update the badge count and list correctly.

- [ ] **Step 5: Commit**

```bash
cd app/frontend
git add src/components/FailedSightings.tsx src/App.tsx src/styles.css
git commit -m "Surface permanently-failed syncs via a badge with retry/discard"
```

---

## Task 5: Full verification pass

**Files:** none (verification only)

- [ ] **Step 1: Run the full backend suite**

Run: `cd app/backend && uv run pytest -v`
Expected: all tests PASS.

- [ ] **Step 2: Run the full frontend suite and build**

Run: `cd app/frontend && npm test && npm run build`
Expected: all tests PASS, build succeeds with no type errors.

- [ ] **Step 3: Manual end-to-end smoke test**

With the backend running (`docker-compose.dev.yml` or however it's normally run locally — see `docs/OPERATIONS.md`) and the frontend dev server proxying to it: take a photo, tap LOG IT, confirm the toast/reset happen with no visible delay, then confirm in the Network tab that `POST /sighting` completes shortly after and the row appears via `/dex`. Then simulate offline (devtools "Offline" throttling), log a sighting, confirm it queues and the toast still fires instantly, then go back online and confirm it flushes automatically via the `online` listener.

- [ ] **Step 4: Commit any fixups**

If verification surfaces any fix, commit it separately with a message describing what was wrong — do not fold fixups into earlier task commits.
