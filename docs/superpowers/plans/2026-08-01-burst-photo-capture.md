# Burst Photo Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let someone capture up to 5 photos of the same dog in one sighting report, instead of exactly one.

**Architecture:** The backend, API client, and offline queue already accept `photos: Blob[]` end-to-end — no changes needed there. This is a frontend-only change to `frontend/src/screens/Capture.tsx`: swap its single-`File` state for an array, add a filmstrip UI to review/remove photos before submitting, and cap the count at 5.

**Tech Stack:** React (TypeScript), Vitest + Testing Library (`@testing-library/react`, `@testing-library/user-event`), jsdom.

## Global Constraints

- Photo cap is 5 per sighting (from the spec — same-session burst only).
- Capture stays single-shot-per-tap (tap shutter → camera → one photo appended); no `multiple` file-picker attribute, per the spec's chosen "repeat shutter, review after" flow (not multi-select from gallery).
- No schema, API (`frontend/src/api.ts`), or offline-queue (`frontend/src/offline/queue.ts`) changes — both already handle `photos: Blob[]` as an array.
- No changes to embeddings or to which photo is used as the map thumbnail (still `photos[0]`, per `frontend/src/components/DogMap.tsx`).
- Spec: `docs/specs/2026-08-01-burst-photo-capture-design.md`.

---

### Task 1: Multi-photo state, filmstrip UI, and array submit in Capture.tsx

**Files:**
- Modify: `frontend/src/screens/Capture.tsx` (whole file — it's ~225 lines, single component)
- Test: `frontend/src/screens/Capture.test.tsx` (new)

**Interfaces:**
- Consumes: `enqueue(input: PostSightingInput): Promise<void>` and `flush(): Promise<void>` from `../offline/queue` (unchanged signatures). `PostSightingInput.photos: Blob[]` from `../api` (unchanged).
- Produces: Nothing else depends on this component's internals — it's a leaf screen.

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/screens/Capture.test.tsx`:

```tsx
// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("../offline/queue", () => ({
  enqueue: vi.fn(),
  flush: vi.fn(),
}));

import { enqueue, flush } from "../offline/queue";
import Capture from "./Capture";

afterEach(cleanup);

function makePhoto(name: string): File {
  return new File(["x"], name, { type: "image/jpeg" });
}

beforeEach(() => {
  vi.mocked(enqueue).mockReset().mockResolvedValue(undefined);
  vi.mocked(flush).mockReset().mockResolvedValue(undefined);
  // jsdom has no createObjectURL; keep it deterministic and traceable to the
  // source file so tests can assert on which photo a thumbnail renders.
  Object.defineProperty(URL, "createObjectURL", {
    value: (b: Blob) => `blob:${(b as File).name}`,
    writable: true,
  });
  Object.defineProperty(URL, "revokeObjectURL", { value: () => {}, writable: true });
});

describe("burst photo capture", () => {
  it("adds each captured photo to a filmstrip instead of replacing the previous one", async () => {
    const { container } = render(<Capture />);
    const input = screen.getByLabelText("capture photo") as HTMLInputElement;

    await userEvent.upload(input, makePhoto("one.jpg"));
    await userEvent.upload(input, makePhoto("two.jpg"));
    await userEvent.upload(input, makePhoto("three.jpg"));

    expect(container.querySelectorAll(".filmstrip-thumb")).toHaveLength(3);
  });

  it("stops accepting new photos once 5 are captured", async () => {
    const { container } = render(<Capture />);
    const input = screen.getByLabelText("capture photo") as HTMLInputElement;

    for (let i = 0; i < 6; i++) {
      await userEvent.upload(input, makePhoto(`p${i}.jpg`));
    }

    expect(container.querySelectorAll(".filmstrip-thumb")).toHaveLength(5);
    expect(screen.getByText("5/5 photos added")).toBeInTheDocument();
    expect(container.querySelector(".filmstrip-add")).not.toBeInTheDocument();
  });

  it("removes only the tapped photo, keeping the others", async () => {
    const { container } = render(<Capture />);
    const input = screen.getByLabelText("capture photo") as HTMLInputElement;

    await userEvent.upload(input, makePhoto("one.jpg"));
    await userEvent.upload(input, makePhoto("two.jpg"));

    await userEvent.click(screen.getByLabelText("Remove photo 1"));

    const thumbs = container.querySelectorAll<HTMLImageElement>(".filmstrip-thumb img");
    expect(thumbs).toHaveLength(1);
    expect(thumbs[0]).toHaveAttribute("src", "blob:two.jpg");
  });

  it("CLEAR ALL empties the whole set, back to the initial shutter", async () => {
    const { container } = render(<Capture />);
    const input = screen.getByLabelText("capture photo") as HTMLInputElement;

    await userEvent.upload(input, makePhoto("one.jpg"));
    await userEvent.upload(input, makePhoto("two.jpg"));
    await userEvent.click(screen.getByText("CLEAR ALL"));

    expect(container.querySelectorAll(".filmstrip-thumb")).toHaveLength(0);
    expect(screen.getByText("Tap to open camera")).toBeInTheDocument();
  });

  it("submits every captured photo, not just the first", async () => {
    render(<Capture />);
    const input = screen.getByLabelText("capture photo") as HTMLInputElement;

    await userEvent.upload(input, makePhoto("one.jpg"));
    await userEvent.upload(input, makePhoto("two.jpg"));
    await userEvent.click(screen.getByText("LOG IT"));

    await waitFor(() => expect(enqueue).toHaveBeenCalledTimes(1));
    const sentPhotos = vi.mocked(enqueue).mock.calls[0][0].photos as File[];
    expect(sentPhotos.map((p) => p.name)).toEqual(["one.jpg", "two.jpg"]);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd frontend && npx vitest run src/screens/Capture.test.tsx`
Expected: FAIL — `Capture.test.tsx` can't find a `capture photo` label (the input has no `aria-label` yet), and the component still only tracks a single `photo`.

- [ ] **Step 3: Rewrite Capture.tsx with array state, filmstrip, and cap**

Replace the full contents of `frontend/src/screens/Capture.tsx` with:

```tsx
import { useRef, useState } from "react";
import { type Condition, type EarNotch, type GeoSource, type Sex } from "../api";
import { enqueue, flush } from "../offline/queue";
import DogSprite from "../components/DogSprite";

const MAX_PHOTOS = 5;

const SEX_OPTIONS: { value: Sex; label: string }[] = [
  { value: "male", label: "♂ male" },
  { value: "female", label: "♀ female" },
  { value: "unsure", label: "unsure" },
];

const EAR_NOTCH_OPTIONS: { value: EarNotch; label: string }[] = [
  { value: "none", label: "none" },
  { value: "left", label: "left" },
  { value: "right", label: "right" },
  { value: "unsure", label: "unsure" },
];

const CONDITION_OPTIONS: { value: Condition; label: string }[] = [
  { value: "healthy", label: "healthy" },
  { value: "injured", label: "injured" },
  { value: "unsure", label: "unsure" },
];

function Chips<T extends string>({
  options,
  value,
  onChange,
}: {
  options: { value: T; label: string }[];
  value: T | null;
  onChange: (v: T | null) => void;
}) {
  return (
    <div className="chip-row">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          className={"chip" + (value === o.value ? " active" : "")}
          onClick={() => onChange(value === o.value ? null : o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function getLocation(): Promise<GeolocationPosition | null> {
  return new Promise((resolve) => {
    if (!("geolocation" in navigator)) return resolve(null);
    navigator.geolocation.getCurrentPosition(
      (pos) => resolve(pos),
      () => resolve(null),
      { timeout: 8000, enableHighAccuracy: true },
    );
  });
}

export default function Capture() {
  const fileRef = useRef<HTMLInputElement>(null);
  const [photos, setPhotos] = useState<File[]>([]);
  const [previewUrls, setPreviewUrls] = useState<string[]>([]);
  const [note, setNote] = useState("");
  const [sex, setSex] = useState<Sex | null>(null);
  const [earNotch, setEarNotch] = useState<EarNotch | null>(null);
  const [condition, setCondition] = useState<Condition | null>(null);
  const [moreOpen, setMoreOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [toast, setToast] = useState<string | null>(null);

  function showToast(msg: string) {
    setToast(msg);
    setTimeout(() => setToast(null), 2600);
  }

  function onFileChosen(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    // Reset now so choosing the same file again still fires a change event.
    if (fileRef.current) fileRef.current.value = "";
    if (!f) return;
    setPhotos((prev) => (prev.length >= MAX_PHOTOS ? prev : [...prev, f]));
    setPreviewUrls((prev) =>
      prev.length >= MAX_PHOTOS ? prev : [...prev, URL.createObjectURL(f)],
    );
  }

  function removePhoto(index: number) {
    setPreviewUrls((prev) => {
      URL.revokeObjectURL(prev[index]);
      return prev.filter((_, i) => i !== index);
    });
    setPhotos((prev) => prev.filter((_, i) => i !== index));
  }

  function reset() {
    previewUrls.forEach((url) => URL.revokeObjectURL(url));
    setPhotos([]);
    setPreviewUrls([]);
    setNote("");
    setSex(null);
    setEarNotch(null);
    setCondition(null);
    setMoreOpen(false);
    if (fileRef.current) fileRef.current.value = "";
  }

  async function submit() {
    if (photos.length === 0) return;
    setSubmitting(true);
    const capturedAt = new Date().toISOString();
    const position = await getLocation();

    const geoSource: GeoSource = position ? "device_gps" : "none";
    const input = {
      photos,
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
      flush().catch(() => {});
    } catch {
      showToast("Couldn't save. Try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="capture-stage">
      <div className="preview-frame">
        {previewUrls.length > 0 ? (
          <img src={previewUrls[previewUrls.length - 1]} alt="captured dog" />
        ) : (
          <div className="placeholder">
            <DogSprite coat="tan" scale={7} />
            <p>Spot an indie? Snap a photo.</p>
          </div>
        )}
        <div className="vf-chrome" aria-hidden="true">
          <span className="vf-corner vf-c1" />
          <span className="vf-corner vf-c2" />
          <span className="vf-corner vf-c3" />
          <span className="vf-corner vf-c4" />
          <span className="vf-rec">
            <i className="vf-dot" />
            REC
          </span>
          <span className="vf-batt">▮▮▮▯</span>
          <span className="vf-stamp">SP · {new Date().toLocaleString()} · GPS</span>
        </div>
      </div>

      <input
        ref={fileRef}
        type="file"
        accept="image/*"
        capture="environment"
        aria-label="capture photo"
        style={{ display: "none" }}
        onChange={onFileChosen}
      />

      {photos.length === 0 ? (
        <div className="shutter-wrap">
          <span className="spot-label">SPOT AN INDIE</span>
          <button className="shutter" onClick={() => fileRef.current?.click()} aria-label="Spot a sighting">
            📷
          </button>
          <p className="hint">Tap to open camera</p>
        </div>
      ) : (
        <>
          <div className="filmstrip">
            {previewUrls.map((url, i) => (
              <div className="filmstrip-thumb" key={url}>
                <img src={url} alt={`captured dog ${i + 1}`} />
                <button
                  type="button"
                  className="thumb-remove"
                  onClick={() => removePhoto(i)}
                  aria-label={`Remove photo ${i + 1}`}
                >
                  ×
                </button>
              </div>
            ))}
            {photos.length < MAX_PHOTOS && (
              <button
                type="button"
                className="filmstrip-add"
                onClick={() => fileRef.current?.click()}
                aria-label="Add another photo"
              >
                +
              </button>
            )}
          </div>
          {photos.length >= MAX_PHOTOS && (
            <p className="hint">
              {MAX_PHOTOS}/{MAX_PHOTOS} photos added
            </p>
          )}

          <div className="note-field">
            <textarea
              rows={2}
              placeholder="Add a note (optional) — e.g. friendly, limping, near the tea stall…"
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
          </div>

          <button
            type="button"
            className="more-toggle"
            onClick={() => setMoreOpen((v) => !v)}
          >
            {moreOpen ? "▾" : "▸"} tell us more (optional)
          </button>

          {moreOpen && (
            <div className="more-fields">
              <div className="field-group">
                <label>sex</label>
                <Chips options={SEX_OPTIONS} value={sex} onChange={setSex} />
              </div>
              <div className="field-group">
                <label>ear-notch (sterilized?)</label>
                <Chips options={EAR_NOTCH_OPTIONS} value={earNotch} onChange={setEarNotch} />
              </div>
              <div className="field-group">
                <label>condition</label>
                <Chips options={CONDITION_OPTIONS} value={condition} onChange={setCondition} />
              </div>
            </div>
          )}

          <div className="actions-row">
            <button className="btn btn-secondary" onClick={reset} disabled={submitting}>
              CLEAR ALL
            </button>
            <button
              className="btn btn-primary"
              onClick={() => submit()}
              disabled={submitting}
            >
              {submitting ? <span className="spinner" /> : "LOG IT"}
            </button>
          </div>
        </>
      )}

      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/screens/Capture.test.tsx`
Expected: PASS — all 5 tests green.

- [ ] **Step 5: Typecheck**

Run: `cd frontend && npm run typecheck`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/screens/Capture.tsx frontend/src/screens/Capture.test.tsx
git commit -m "feat(capture): allow up to 5 photos per sighting

Repeated shutter taps append to a filmstrip instead of replacing the
prior photo; each thumbnail can be removed individually, CLEAR ALL
resets the set, and submit sends every captured photo. The API,
offline queue, and backend already accepted photos as an array, so
this is a capture-screen-only change."
```

---

### Task 2: Filmstrip styling

**Files:**
- Modify: `frontend/src/styles.css` (append near the existing `/* --- Capture / Spot screen --- */` section, after `.hint`)

**Interfaces:**
- Consumes: class names introduced in Task 1 — `.filmstrip`, `.filmstrip-thumb`, `.filmstrip-add`, `.thumb-remove`. Existing tokens `var(--accent)`, `var(--line)`, `var(--panel)`, `var(--bg)`, `var(--ink)`, `var(--muted)` (already used elsewhere in this file).
- Produces: nothing downstream depends on these styles besides the visual result.

- [ ] **Step 1: Add filmstrip CSS**

In `frontend/src/styles.css`, insert directly after the `.hint { ... }` rule (currently the line right after `.shutter[disabled]` and `.spot-label`):

```css
.filmstrip {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  width: 100%;
}

.filmstrip-thumb {
  position: relative;
  width: 56px;
  height: 56px;
  border: 3px solid var(--line);
  border-radius: 4px;
  overflow: hidden;
  image-rendering: pixelated;
}

.filmstrip-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.thumb-remove {
  position: absolute;
  top: -2px;
  right: -2px;
  width: 18px;
  height: 18px;
  border: 2px solid var(--line);
  border-radius: 50%;
  background: var(--ink);
  color: var(--bg);
  font-size: 0.7rem;
  line-height: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  padding: 0;
}

.filmstrip-add {
  width: 56px;
  height: 56px;
  border: 3px dashed var(--line);
  border-radius: 4px;
  background: var(--panel);
  color: var(--accent);
  font-size: 1.4rem;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
}
```

- [ ] **Step 2: Manually verify in the dev server**

Run: `cd frontend && npm run dev`

Open the app, go to the capture/spot screen, and tap the shutter 3 times (or use a browser file picker if no camera is available). Confirm:
- Each tap adds a new thumbnail to the filmstrip rather than replacing the preview.
- The big preview above shows the most recently added photo.
- Tapping a thumbnail's "×" removes just that photo.
- After 5 photos, the dashed "+" add button disappears and "5/5 photos added" shows.
- "CLEAR ALL" empties everything back to the initial shutter screen.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/styles.css
git commit -m "style(capture): add filmstrip thumbnail styling"
```

---

## Self-Review Notes

- **Spec coverage:** array state + cap 5 (Task 1 Step 3), repeat-shutter flow with no `multiple` attribute (Task 1 Step 3, input unchanged aside from `aria-label`), per-photo removal (Task 1 Step 3 `removePhoto`, tested in Task 1 Step 1), CLEAR ALL full reset (Task 1 Step 3 `reset`, tested), submit sends full array (Task 1 Step 3 `submit`, tested), no API/queue/backend changes (confirmed against `api.ts`/`queue.ts` — untouched), no embeddings/thumbnail changes (out of scope, nothing in this plan touches `DogMap.tsx` or backend). All spec sections have a corresponding task.
- **Placeholder scan:** none — every step has real code or a runnable command.
- **Type consistency:** `PostSightingInput.photos: Blob[]` (from `api.ts`, unchanged) accepts `File[]` since `File extends Blob`; `enqueue`/`flush` signatures unchanged and match Task 1's usage.
