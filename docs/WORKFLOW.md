# Capture workflow: before and after re-ID

What one photo upload did at `cf276c4`, what it does now, and why each change
was made. Companion to `AGENTS.md` (which describes the current system only).

---

## Before

```mermaid
flowchart TD
    A["Capture — photo only<br/>accept=image/*"] --> B["IndexedDB queue<br/>(offline-first)"]
    B --> C["POST /sighting<br/>multipart"]

    subgraph SYNC["synchronous — user is waiting"]
        C --> D["process_photo<br/>EXIF strip · JPEG q95 · thumb q80 · phash"]
        D --> E["S3: original + thumb<br/>.jpg keys · one endpoint"]
        E --> F["INSERT sighting + photos<br/>(one transaction)"]
        F --> G["201"]
    end

    G --> H["BackgroundTasks"]
    H --> I["dog_confidence<br/>YOLOv8n"]
    I --> J[("sightings.dog_confidence")]
    J --> K["END"]

    L[("embeddings<br/>match_proposals<br/>individuals<br/>confirmations")]

    style SYNC fill:#0b3d2e22,stroke:#2d7a5f
    style K stroke:#b91c1c
    style L stroke:#b91c1c,stroke-dasharray: 5 5
```

The four tables on the right existed from migration `0001`. **No committed code
had ever written a row to any of them.** A photo went in, received a
dog-confidence score, and stopped. There was no embedding, no candidate search,
no notion of an individual animal.

---

## Now

```mermaid
flowchart TD
    A["Capture — photo(s) <b>or</b> a clip"] --> B["IndexedDB queue<br/>owns the bytes, clips included"]
    B --> C["POST /sighting<br/>multipart"]
    C --> C2{"clip?"}
    C2 -- yes --> C3["extract_diverse_frames<br/>~2 fps · phash-diverse · ≤12<br/>clip never stored"]

    subgraph SYNC["synchronous — user is waiting"]
        C2 -- no --> D
        C3 --> D["process_photo<br/>EXIF strip · <b>WebP q90</b> · thumb q80 · phash<br/><i>off the event loop</i>"]
        D --> E["S3: original + thumb<br/><b>.webp</b> keys · <b>write vs presign endpoints</b>"]
        E --> F["INSERT sighting + photos"]
        F --> G["201"]
    end

    G --> H["BackgroundTasks"]

    subgraph BG["background — nothing blocks the response"]
        H --> I["animal_confidence<br/><b>YOLO26x</b> → dog + cat"]
        H --> J["_embed_and_save"]
        J --> K{"best_animal_box<br/>dog or cat?"}
        K -- no --> L["no row written<br/>sighting stays unmatchable"]
        K -- yes --> M["crop +10% margin"]
        M --> N["MiewID-msv3<br/>2152-d, L2-normalised"]
        N --> O[("embeddings.vec_miew")]
    end

    O --> P["resolve_sighting<br/>PostGIS 1km → HNSW → exact re-rank<br/><i>runs here, once — GET /match is read-only</i>"]
    P --> Q{"top similarity"}
    Q -- "≥ 1.01 (unreachable)" --> R["auto-link"]
    Q -- "≥ 0.71 (look-alike ceiling)" --> S["match_proposals<br/>+ suggest_video on thin evidence"]
    Q -- else --> T["unmatched"]
    S --> U["human verdict<br/>POST /proposal/{id}"]
    U --> V[("individuals + confirmations")]

    style SYNC fill:#0b3d2e22,stroke:#2d7a5f
    style BG fill:#3d2e0b22,stroke:#7a5f2d
    style L stroke:#b91c1c
    style V stroke:#2d7a5f
```

---

## Stage by stage

| Stage | Before | Now | Why |
|---|---|---|---|
| Capture | `accept="image/*"` | **photo(s) or a short clip** | Frames per individual is the biggest measured lever: 1 → 8 frames takes top-1 from 37% to 83% |
| Encode | JPEG q95 / thumb q80 | **WebP q90** / thumb q80 | −0.2 accuracy points, 66% smaller (674 kB vs 2001 kB on a 12 MP capture) |
| S3 keys | `.jpg` | `.webp` | Old objects keep their keys and formats; both coexist |
| S3 endpoint | one | **write vs presign split** | SigV4 signs the Host, so a URL signed for the internal host 403s in a browser |
| Animal gate | YOLOv8n | **YOLO26x**, dog *and* cat | v8n scored a clearly visible dog at 0.021 where 26x gives 0.800 |
| Embedding | none | **MiewID-msv3**, 2152-d, on the animal crop | Whole-frame embeddings match other streets, not other dogs |
| Candidate search | none | PostGIS 1 km → HNSW → exact re-rank | HNSW caps at 2000 dims; MiewID is 2152, so ANN runs on a `halfvec` cast and the shortlist is re-scored at full precision |
| Identity | none | human verdict only | See thresholds below |
| Basemap | OSM raster | CARTO Positron / Dark Matter | Road-atlas detail competed with the sightings |
| Pins | square | round | Matches the cluster bubbles |

---

## Thresholds, and why nothing merges itself

`reid_auto_merge_min` is **1.01** — deliberately unreachable. Every identity in
the system comes from a person saying "same dog".

That is not caution, it is what the data supports. Measured on 30 sightings of
10 dogs pushed through this API, session-disjoint:

| prior photos of the dog | 1 | 2 | 4 | 8 | 32 | 64 | 4,767 |
|---|---|---|---|---|---|---|---|
| top-1 | 37% | 43% | 67% | 83% | 87% | 87% | 94.9% |

Two things follow:

**94.9% is a dense-gallery number.** It is the research repo's figure over a
47,671-frame gallery. A dog with one prior photo is a ~40% problem. Do not quote
94.9% as what a new contributor will experience.

**d′ stayed flat at ~0.85 across every gallery size** (genuine ≈0.35, impostor
≈0.245). More photos improve *ranking* — the odds that some stored frame of the
right dog is nearest — and never improve *separation*. Proposal precision was
18–36% at every cut-off from 0.20 to 0.50. No amount of collected data rescues a
fixed threshold; only a human looking at a ranked candidate does.

The pipeline is faithful to the offline research path — on identical frames this
API scored **50%** top-1 against the offline path's **46.7%**. Gaps to benchmark
numbers are the gallery, not the service.

### These numbers came from a lab, and expire

Everything above was measured on the **Dognosis v2** clips: 10 dogs in a single
indoor scent-detection room — constant background, constant lighting, fixed
camera positions, animals in harnesses, and a population skewed to a few breeds.
That last part is why look-alikes dominate: several beagles, several black labs.

Street sightings share none of it. Background, light, distance, angle and
weather all vary; free-roaming indies vary far more in coat, markings and size
than a kennel of beagles; and the population is far larger, so look-alikes are
rarer per pair but more numerous overall. Those pull in opposite directions and
**neither has been measured**.

So `reid_propose_min = 0.71` is a placeholder shaped by a lab dataset, not a
property of MiewID or of dogs. **Recalibrate weekly as real sightings arrive.**
The inputs already exist and no new experiment is needed:

| what | where |
|---|---|
| the label | `confirmations.verdict` (`same` / `different`) |
| what the model said | `match_proposals.score` for that proposal |
| how much evidence | count of `embeddings` per sighting |

Join those, plot the two distributions, and pick the operating point you want on
the precision/recall curve. Until that has been done at least once on real
verdicts, treat the current default as a starting position rather than evidence.

---

## Video capture

For a long time there was no video option because PR #3 (`feat/video-capture`)
was never merged — and **it cannot be merged**: that branch was cut before the
repository's history was rewritten, so it shares no common ancestor with `main`
(different root commit, and it carries only migration `0001`). GitHub will never
offer a clean merge for it.

Its two commits are cherry-picked here instead. `app/video.py` decodes a clip
with `imageio` + `imageio-ffmpeg` (a bundled static binary, so the slim
container needs no apt package), subsamples to ~2 fps, runs each frame through
the ordinary `process_photo`, and keeps a phash-diverse subset — up to 12
frames. The clip itself is **never stored**; only the frames it yielded.

The capture screen's half of that branch was discarded rather than merged: it
predates the multi-photo refactor from PR #6 and conflicted in eight places
against a component that no longer exists in that shape. The video option was
rebuilt on the current screen instead, including the offline queue, which now
reads a clip's bytes at capture time exactly as it does for photos — a camera
video File on iOS is the same purgeable handle, only larger.

Measured live on a 2.8 s, 30 fps clip: 201 in 0.7 s, 6 diverse frames stored,
5 embedded (one frame had no animal in it, correctly skipped),
`dog_confidence` 0.969, `attrs.source = "video"`.

**Matching had to change to make video worth anything.** `resolve_sighting`
originally queried with a sighting's *first* embedding, so a six-frame clip
searched with one frame and the other five were dead weight on the query side.
On that live clip the single-frame query ranked the wrong dog first and the
right dog fourth; querying with all six put the right dog first **and** second:

| | top candidate | rank of the true dog |
|---|---|---|
| first frame only | cheetah 0.4158 | 4th (0.3052) |
| max over 6 frames | **luna 0.5401** | **1st and 2nd** |

`find_candidates` now takes every embedding of the query sighting, runs one
indexed ANN probe per frame, and scores each candidate by its best match against
any frame. A candidate is the same animal from a different angle; the question
is "have we seen this dog", not "have we seen this pose".

One correction still outstanding: frame selection uses **phash**, which compares
whole scenes. Two frames of the same dog against the same wall look
near-identical to phash while differing usefully in embedding space. Selecting on
embedding diversity would be better now that the embedder is in-tree. Sampling
the middle 80% of the clip would help too — detection succeeds 93% mid-clip
against ~39% at the edges.

---

## What existing data does on deploy

**Schema migrates itself.** `deploy/entrypoint.sh` runs `alembic upgrade head`
before uvicorn, so `0005` and `0006` apply on the next deploy. pgvector comes
from the `pgvector/pgvector:pg16` image (0.8.6; `halfvec` needs ≥0.7.0). Both
new columns are nullable.

**S3 needs nothing.** Format is per-object and the key lives in `photos.s3_key`.
Existing `.jpg` objects keep serving; only new uploads are `.webp`.

**Existing photos need a backfill, and there is now a script for it:**
`backend/scripts/backfill_embeddings.py`. Nothing embeds them automatically, and
`find_candidates` filters on `vec_miew IS NOT NULL`, so until it runs the whole
existing corpus is invisible to matching — which, given that accuracy *is*
gallery density, starts the system at the 1-photo-per-dog end of the table above
while the photos that would fix it sit unread in S3.

```
uv run python scripts/backfill_embeddings.py --dry-run   # count first
uv run python scripts/backfill_embeddings.py --resolve   # embed, then match
```

Serial by design: embedding is CPU-bound ONNX on the same box that serves
requests, so a parallel backfill would compete with live uploads for cores
(`--sleep` throttles it further). Resumable — it only selects photos with no
vector and upserts on `(photo_id, model)`, so interrupting it loses at most the
photo in flight. One quirk: a photo with no detectable animal gets no row at all
(a whole-frame vector would pollute candidate search), so it is re-examined on
every run and the pending count never reaches zero on a corpus with dogless
photos. That is deliberate — those should be retried after a detector upgrade.

**`dog_confidence` becomes incomparable across the deploy** — old rows scored by
YOLOv8n, new rows by YOLO26x, and the two disagree substantially (on 29 varied
photos: dogs 14/17 vs 9/17, cats 10/12 vs 1/12). Any filter on that column will
treat two different populations as one.

**A failed migration would take the site down**, not merely leave it un-updated:
`entrypoint.sh` is `set -e`, so a migration error means uvicorn never starts and
the replaced container is already gone. Both new migrations add `CHECK`
constraints, which Postgres normally validates against every existing row. Both
are therefore declared **`NOT VALID`**: new and updated rows are still enforced,
the scan is skipped, and no pre-existing data can abort the boot. Validate them
deliberately later, under a lock that does not block writes:

```sql
ALTER TABLE embeddings      VALIDATE CONSTRAINT ck_embeddings_miew_vec;
ALTER TABLE match_proposals VALIDATE CONSTRAINT ck_match_proposals_has_target;
```

**The models are fetched on boot.** They are gitignored and not baked into the
image, so `git pull` never brings them and a fresh container has none — in which
case both ML tasks raise, get caught, and every upload saves with no embedding:
re-ID looks deployed and does nothing. `entrypoint.sh` now runs
`scripts/fetch_models.py` first, pulling the pre-exported ONNX from object
storage onto a named volume, so it happens once per box rather than once per
deploy. Deliberately non-fatal — a fetch failure should degrade re-ID, not take
the site down — so the state is reported instead:

```
GET /health  →  {"status":"ok","reid":"ready"|"degraded","models":{...}}
```

Seed the bucket once from a machine that has run the export scripts:
`uv run python scripts/fetch_models.py --upload`.

Baking the weights into the image was rejected: ~430 MB on every deploy, and
MiewID declares no upstream licence, which makes shipping it inside a
distributable artifact an open question. Exporting on the box is impossible by
design — that needs torch, which the runtime image deliberately excludes.

---

## Local development ports

Every default port collided on the machine this was built on, so the local stack
runs remapped. **No committed default changed** — see `AGENTS.md` for the table
and where each override lives (`docker-compose.dev.local.yml`, `backend/.env`,
`frontend/vite.config.local.ts`, all gitignored). In short: Postgres 5433, MinIO
9002/9003, API 8099, Vite 5174 over HTTPS for phone testing.

Two traps in there: Compose *appends* `ports` lists, so an override needs
`ports: !override` or the original binding survives and still collides; and the
API port has to match the Vite proxy target, because the frontend proxies
server-side rather than from the browser.

---

## Concurrency

The connection pool is shared by request handlers, which hold a connection for
the whole request via the `get_conn` dependency, and by the background tasks
that embed and match after the response. asyncpg defaults to `max_size=10`,
which is not enough for both: 16 concurrent uploads produced exactly 10 served
and 6 that were never handed to the app at all, suspended on `pool.acquire()`
until the client gave up. `db_pool_max` now defaults to 30 and all 16 return
201 in ~1.7 s.

Worth remembering when something similar appears: a suspended coroutine has no
thread stack, so a `faulthandler` dump shows only idle workers and no
application frames. Every signal will say the server is doing nothing while
requests hang. Count the requests that *did* succeed — a number matching a pool
or limiter size is the answer.
