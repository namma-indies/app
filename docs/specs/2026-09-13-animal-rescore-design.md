# Rescoring the corpus, and keeping non-animals off the shared map — design

_Author: Claude, 2026-09-13. Product calls (filter at display rather than gate
at capture, the contributor is told but has no appeal yet, any animal counts —
cat or dog) made by Akash; this doc covers the technical shape._

Answers issue #67. Deliberately does **not** pick the confidence threshold —
that is phase 2, and it happens against rescored numbers.

## Goal

Make `dog_confidence` mean one thing, then use it: a photo with no animal in it
stays saved and stays in its owner's `/dex`, but does not appear on `/map`,
`/dogs` or in the public counts — and its owner is told why.

## The trap, restated

`sightings.dog_confidence` is not comparable across rows. Older rows were
scored by YOLOv8n, newer ones by YOLO26x, and the two disagree badly: v8n
scored a clearly visible dog at **0.021** where 26x gives **0.800**
(`docs/WORKFLOW.md`). **Which model produced a given score is recorded
nowhere.** So filtering on the column today would silently hide real dogs whose
only crime was being uploaded early.

That fixes the order of work, and it is the reason this spec exists rather than
a one-line `WHERE`:

1. Rescore every photo with the current detector.
2. Record which detector did it.
3. *Then* choose a threshold.

## Scope

**In:**

1. A `detections` table at `(photo_id, model)` grain — dog and cat confidence
   per photo, per model.
2. `sightings.animal_confidence` — the denormalised number the read surfaces
   filter on.
3. A resumable `rescore_photos.py`, shaped like the embeddings backfill.
4. The filter itself, wired into `aggregates.COUNTABLE_SIGHTING` but **inert**
   until a threshold is configured.
5. `/dex` telling the owner when one of their sightings is off the shared map,
   and why.
6. Deleting the dead YOLOv8n detector.

**Out, and why:**

| Not building | Why |
|---|---|
| The threshold value | #67's whole argument is that it must be chosen against rescored numbers. Shipping a number derived from the mixed corpus would repeat the mistake the spec is fixing. |
| A capture-time reject gate | Migration `0002` exists *because* this was built and reverted. The PWA captures through a file input, so iOS never writes those frames to the camera roll — the server copy a gate discards is the only copy in existence. A wrong call must cost a label, never the photo. |
| An appeal / "this really is a dog" button | #54. It needs a queue source and a surface; being told is the agreed first step. |
| Writing the verdict into `review_status` | See below — that column means "a person ruled". |
| Deleting anything | #54 and #63 own deletion. Nothing here removes a row or an object. |
| Moving inference to a GPU | #48 / PR #64. Three minutes of CPU does not need a GPU; see *Where it runs*. |
| Persisting the box here | `embeddings.bbox` already holds it for every photo with an animal. |

## Part 1 — where the number lives

```sql
CREATE TABLE detections (
    photo_id   uuid NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    model      text NOT NULL,
    dog        real NOT NULL,
    cat        real NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (photo_id, model)
);
```

Per photo, per model — the same shape `embeddings` has used since `0001`, and
chosen over a second column on `sightings` for three reasons that are all
verifiable in the current tree:

**It gives the rescore a resume key.** `backfill_embeddings.py` resumes with
`LEFT JOIN embeddings ON photo_id AND model`. A column on `sightings` has no
resume key except `IS NULL` — and that script's own docstring documents where
that leads: a photo with no animal never gets a row, so the pending count never
reaches zero and every run re-examines it. Keying on the model makes "already
scored by *this* detector" an exact question.

**It makes the next detector swap free.** The reason #67 exists is that nobody
recorded which model scored what. `model` is that record. A future swap adds
rows rather than invalidating them, and the old numbers stay readable as
history instead of becoming landmines.

**Cats come along for nothing.** `analyse()` already returns `cat_confidence`
and `routes/sighting.py` discards it at line 79. Persisting both makes "not a
dog" and "not an animal" two queries over one table rather than a second
migration later — which matters because the product call here is *any animal
counts*, and that is the kind of call that gets revisited.

`DETECTOR_NAME = "yolo26x"` goes in `detect_reid.py` next to `_MODEL_PATH`,
mirroring `embed.MODEL_NAME = "miewid-msv3"`, and is what gets written to
`detections.model`.

## Part 2 — the number the surfaces read

```sql
ALTER TABLE sightings ADD COLUMN animal_confidence real;
```

Defined as: **the maximum over the sighting's photos of `max(dog, cat)`, under
the current detector only.** Denormalised deliberately — `/map` and `/dogs` are
geo and count queries, and `COUNTABLE_SIGHTING` is a SQL fragment pasted into
several of them, so a per-row join to `detections` would have to be threaded
through every caller. One column keeps the filter a predicate.

Written in two places, both of which already exist as functions: the capture
path (`_save_dog_confidence`, renamed) and the rescore script.

**NULL means visible.** A never-scored sighting is shown, not hidden. This is
the same fail-open contract `0002` established — a detector failure costs a
label and nothing else — and it is what makes the filter safe to deploy before
the rescore finishes. The 9 currently-unscored sightings do not vanish the
moment this lands.

`sightings.dog_confidence` becomes dead. It is write-only today — grep finds it
only in write sites, migrations and tests, with no reader in the backend or the
frontend — so nothing breaks by superseding it. **It is dropped in a separate,
later migration, after the rescore has actually run in production.** Dropping
it in the same migration would destroy the only record of the old scores before
the new ones exist to compare against.

## Part 3 — the filter

`aggregates.py` already declares itself the owner of what counts as a sighting,
and `/map` and `/dogs` import that rather than restating it. So the filter is
one predicate in one place:

```python
COUNTABLE_SIGHTING = (
    "s.review_status = 'valid' "
    f"AND (s.animal_confidence IS NULL OR s.animal_confidence >= {_MIN})"
)
```

where `_MIN` is `settings.animal_confidence_min`, formatted as a float literal
at import. `/dex` filters by `observer_id` and does not use this constant, so
"yours stays in your dex" holds by construction rather than by remembering.

### Two things this deliberately does not do

**It does not write `review_status`.** That column, and `reviewed_at`, mean *a
person ruled on this* — #66 built the two-reporter moderation flow on exactly
that reading, and a model writing the same column would erase it. A sighting
can be off the map because a moderator hid it, or because the detector found no
animal, and those stay independently true and independently reversible.

**It ships inert.** `animal_confidence_min` defaults to `0.0`, which passes
everything. After the rescore runs in production we read the real histogram and
set the value in the environment — no redeploy, and no number chosen from data
#67 tells us not to trust. Phase 2 is that one environment variable.

Consequence to accept: the setting is read at import, so changing it needs a
restart. That is already true of every other setting and a deploy restarts
anyway.

## Part 4 — rescoring

`backend/scripts/rescore_photos.py`, modelled on `backfill_embeddings.py`:

- Pending set: `photos LEFT JOIN detections ON photo_id AND model = $DETECTOR`
  where the row is missing.
- Serial, one photo per transaction, safe to interrupt and re-run.
- `--dry-run`, `--limit`, `--sleep`, matching the existing script's flags.
- Recomputes `sightings.animal_confidence` for each sighting it touches, once
  that sighting's photos are all scored.

**Where it runs.** On the box that serves requests, same as the embeddings
backfill. The corpus is order-100 photos, and one YOLO26x pass measures ~0.9 s
on the 2-core production shape (#49) — **single-digit minutes of CPU** for the
whole rescore. `--sleep` throttles it
further if it runs during the day. This is not a reason to wait for #48.

**`--embed`.** 20 photos currently have no MiewID embedding. Rescoring and then
running `backfill_embeddings.py` means two detector passes over the same
bytes — precisely the waste #49 removed from the capture path. With `--embed`,
the script reuses the same `Analysis` to fill a missing embedding. The saving
is small at this corpus size; the reason to do it is that the alternative
reintroduces a mistake this repo has already paid to fix once.

## Part 5 — telling the contributor

`/dex` gains two fields per sighting:

- `on_map: bool`
- `off_map_reason: "reported" | "hidden" | "no_animal" | null`

`reported` and `hidden` restate `review_status`; `no_animal` is the new one.
While the threshold is `0.0`, `no_animal` never occurs — the field lands with
the mechanism and starts firing when phase 2 sets the threshold.

The frontend already has the surface: `Dex.tsx:216` renders an `.under-review`
line for any sighting whose `review_status` is not `valid`, with a comment
saying being told is the point. This adds a third case to that block —
*"NOT ON THE SHARED MAP · NO ANIMAL DETECTED"* — rather than a new component.

No appeal button. #54.

## Part 6 — deleting the old detector

`app/detect.py` still holds the YOLOv8n scorer, and it is dead: `analyse()` and
everything downstream use `detect_reid` (YOLO26x). What survives of `detect.py`
is `load_upright`, which three modules import.

Removed: `dog_confidence()`, `_MODEL_PATH`, `_DOG_CLASS`, `_NUM_ATTRS`,
`_letterbox`, `DOG_CONF_THRESHOLD`, and the 12 MB `app/ml/yolov8n.onnx`.
`test_detect.py` imports that `_letterbox`; its letterboxing assertions move to
`test_detect_reid.py`, which exercises the one that is actually in the path.
`app/ml/NOTICE.md` and `scripts/fetch_models.py` drop their v8n entries.
`detect.py` keeps its name and becomes the image-loading module, with a
docstring that says so.

`DOG_CONF_THRESHOLD = 0.25` deserves its own line here. It is **v8n-calibrated**
and currently feeds nothing but a log message at `routes/sighting.py:214`. If it
survived into this work it would become the 26x threshold by inheritance —
a number carried over from a detector that scored a real dog at 0.021. Deleting
it is part of making one number mean one thing. `test_sighting.py:128` asserts
against it and moves to the new setting.

## Interaction with PR #64 (GPU worker)

#64 is an open draft that queues analysis to a leased GPU worker. It overlaps
this work in three places and **does not supersede it**:

- It still writes a single `sightings.dog_confidence` with no record of which
  model produced it. It carries `cat_confidence` over the wire and drops it at
  the database. The comparability problem survives the move to a GPU.
- It edits `_analyse_and_save` and `_save_dog_confidence` in
  `routes/sighting.py` — the same functions this touches.
- It adds `sightings.processing_state` with a `no_animal` value, which is
  adjacent to Part 5 but answers a different question: `processing_state` is
  *how far the pipeline got*, `off_map_reason` is *why this is not on the map*.

Agreed approach: **build this on `main` now.** Whoever merges second reconciles.
For that to be cheap rather than painful, this work must leave the GPU path one
obvious insertion point — the worker's completion handler writes `detections`
rows and recomputes `animal_confidence`, exactly as the capture path does, and
that is the only thing #64 has to change. A note goes on #64 saying so.

Migration numbering: `main`'s head is `0012_individual_names`, so this is
`0013_detections`. #64's branch already carries a `0013_merge_media_jobs`
revision id — different id, same filename number. Alembic resolves it with a
merge revision at integration time; it is not a conflict in the tree.

## Testing

| What | How |
|---|---|
| Capture writes both confidences | Post a sighting, assert a `detections` row at `(photo_id, "yolo26x")` with dog and cat set. |
| Sighting number is the max | Two photos, different scores → `animal_confidence` is the higher, over both classes. |
| Never-scored stays visible | `animal_confidence IS NULL` with a non-zero threshold → still counted by `aggregates`. |
| Below threshold drops off the shared surfaces | With the threshold set, the sighting leaves `/map` and the `/dogs` counts, and `review_status` is untouched. |
| …and stays in `/dex` | Same sighting, owner's `/dex`: present, `on_map: false`, `off_map_reason: "no_animal"`. |
| A moderator verdict is unaffected | Hiding and unhiding still works on a sighting the detector scored high, and vice versa. |
| Rescore is resumable | Run twice; the second run scores nothing. `--dry-run` writes nothing. |
| Detector failure fails open | `analyse` raising leaves no `detections` row, `animal_confidence` NULL, sighting saved and visible. |

## Phase 2

Run the rescore in production, read the histogram of `detections.dog` and
`detections.cat` under `yolo26x`, pick `animal_confidence_min`, set it in the
environment. Then drop `sightings.dog_confidence`.
