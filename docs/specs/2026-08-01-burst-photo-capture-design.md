# Burst photo capture

## Why

A single photo per sighting limits how well a re-ID model can learn a dog's
appearance. Letting someone capture several photos in one sitting — tagged to
the same sighting, and therefore the same individual once matched — gives the
model multiple angles of the same dog to train on.

## Scope

Same-session burst only: multiple photos captured back-to-back during one
report, submitted together as one sighting. Not in scope: attaching more
photos to an already-identified dog later, or across separate visits/observers.
Not in scope: video capture (considered and rejected for now — would need a
new frame-extraction pipeline, either client-side canvas work or a server-side
ffmpeg dependency, plus handling motion-blurred frames and much larger
uploads on likely-spotty mobile data; the training benefit is the same as
burst photos with meaningfully more risk).

## Current state

The backend, API client, and offline queue already support multiple photos
per sighting:

- `photos` table keys on `sighting_id` (not unique) — schema already allows
  many photos per sighting.
- `POST /sighting` already accepts `photos: list[UploadFile]` and inserts one
  row per file (`backend/app/routes/sighting.py`).
- Frontend API layer already models `photos: Blob[]` end-to-end, including
  the offline queue (`frontend/src/api.ts`, `frontend/src/offline/queue.ts`).

The only place hard-locked to one photo is the capture screen itself
(`frontend/src/screens/Capture.tsx`): `photo: File | null` state, a file
input read via `files?.[0]`, and `submit()` wrapping it as `photos: [photo]`.

## Design

**State:** `photo: File | null` → `photos: File[]`, capped at 5.
`previewUrl` → derived preview URLs per photo.

**Flow:**
- Tap shutter → camera opens → captured photo appends to `photos` and
  becomes the big preview.
- A filmstrip of thumbnails appears below the preview once ≥1 photo exists.
  Each thumbnail has a small "×" to remove that one photo.
- The shutter button stays visible/tappable alongside the filmstrip so the
  user can keep adding, up to 5. At 5 it disables with a "5/5 added" hint.
- "RETAKE" is renamed "CLEAR ALL" and resets the whole set back to the empty
  state. Removing a single photo is done via its thumbnail's "×", not this
  button.
- "LOG IT" stays disabled until at least 1 photo exists (unchanged).

**Submit:** sends `photos` as the full array — the API, offline queue, and
backend already accept this shape with no changes needed.

## Out of scope / explicitly deferred

- Cross-visit photo addition to an existing individual.
- Video capture and frame extraction.
- Any change to embeddings (still one row per photo — unchanged) or to which
  photo is used as the map thumbnail (still `photos[0]`, unchanged).
