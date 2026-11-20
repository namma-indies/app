# Multi-animal grouping with confidence

**Status:** built on `launch/multi-animal-grouping`, 2026-11-19.
**Decision owner:** Akash (product), 2026-11-19.

## The decision

> Publish confident groupings automatically; ask the person (one tap) only
> where we're not confident.

This **reverses** the 2026-09-15 rule (issue #76, PR #87) that "a model score
may never publish a merge", under which any capture where one species showed
two animals in a frame and reappeared went to the person with every box as its
own animal. Kept from before:

- Two boxes in the **same frame** are never the same animal.
- Nothing here links boxes **across captures**; `matching.resolve_sighting`'s
  auto-merge stays unreachable (`auto_merge_min=1.01`).

## What the person sees

- **Confident clip or burst:** no review step. The capture publishes as
  `ready` on the worker's completion and appears in the Journal like any
  other upload.
- **Unsure clip or burst:** the Journal entry says "Check the animals". The
  sheet shows **one card per animal** (letter A/B/C and a colour that follows
  the animal into every frame), the animal's clearest crop as the card face, a
  dog/cat toggle ("Dog B might be a cat. Tap Cat if so." when the vote was
  close), and the animal's frames as a collapsible strip in time order (open
  on the unsure ones). The footer's one tap is "Looks right · publish N
  animals"; once anything is regrouped it reads "Publish N animals".
  Actions: same animal as… (merge), move one box to another or a new animal,
  "Not an animal · leave out" per animal or per box, species toggle.

## How grouping works (technical)

`app/tracking.py` (worker and CPU fallback both): frames in time order; each
box scored against every animal so far by MiewID appearance (cosine mapped
from 0.20..0.80 onto 0..1) and, in clips, motion (centre shift in box-sizes
per second, or IoU). One-to-one greedy assignment per frame. Each link and
each "new animal" decision gets a confidence; species is a
confidence-weighted vote over the animal's boxes using per-crop dog/cat
scores. A capture is confident only when every decision is and no species
vote is close.

### Thresholds (`settings.grouping_*`, sent to the worker with every job)

| Setting | Default | Meaning |
|---|---|---|
| `accept_min` | 0.35 | below this a box starts a new animal |
| `confident_min` | 0.65 | a link this strong... |
| `margin_min` | 0.20 | ...that beats its runner-up by this is confident |
| `new_animal_max` | 0.20 | a new animal is confident only if every earlier one scored below this |
| `cosine_floor` / `cosine_ceiling` | 0.20 / 0.80 | MiewID cosine read as "no resemblance" / "same look" |
| `appearance_weight` | 0.75 | appearance vs motion |
| `motion_speed_scale` | 2.0 | box-sizes/s at which motion scores 1/e |
| `motion_horizon_s` | 3.0 | gap beyond which motion says nothing |
| `species_mismatch_penalty` | 0.15 | off a link whose boxes carry different labels |
| `species_share_min` | 0.70 | species vote share below this is "uncertain" |

**None of these are calibrated on street footage.** They were set so that
clearly different-looking animals publish and look-alikes, crossings of
similar animals, missing embeddings, middling resemblances (cosine ~0.4-0.6)
and dog/cat toss-ups ask. Validate on staging (below) before trusting them.

### Frames and budget

A clip is sampled into up to 48 evenly spaced candidates, near-duplicates
(phash within 4 bits of the last kept) dropped, then thinned evenly to at most
`clip_tracking_frames` = 12 (worker `Limits`). Only frames that still show an
animal are stored; their screening signals are folded into the first kept
frame. Over 96 boxes, each animal keeps its best views (confidence x size)
instead of the job failing.

### Wire and storage

- Claim adds `accepts: ["grouping"]` and `grouping_policy`. A worker sends
  `tracks` (per-animal species, uncertainty, confidence, representative box,
  reasons), `grouping_policy` and per-box `dog_score`/`cat_score` only when
  the claim accepts them. **Deploy the API first.** The API also holds a
  capture whose worker says "ready" if any track is unsure.
- Migration `0049_multi_animal_grouping`: `capture_tracks`, and
  `sightings.cover_photo_id` (the representative crop, shown first in the
  Journal, map and dog cards).
- `AnimalGroup.species` (optional) carries the person's or the vote's
  species; `CaptureStatus.animal_hints` sits beside `groups` so older clients
  that echo groups back are not rejected.

## Validating on staging (for Aswin)

1. Deploy API (migration 0049), then the worker.
2. Record clips: one dog walking; two clearly different dogs crossing; two
   look-alike dogs (siblings) crossing; a dog that leaves and comes back; a
   dog and a cat; a single indie the detector tends to call a cat.
3. For each, check `processing_state` and
   `SELECT track_id, species, species_uncertain, species_share, confidence,
   confident, reasons FROM capture_tracks WHERE capture_id = ...`.
4. Wrong auto-publishes mean thresholds are too loose: raise
   `GROUPING_CONFIDENT_MIN` / `GROUPING_MARGIN_MIN`. Clearly-easy clips held
   for review mean too strict: lower them. No worker redeploy is needed.
