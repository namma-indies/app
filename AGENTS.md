# AGENTS.md

Orientation for AI coding agents working in this repo. Vendor-neutral companion
to `CLAUDE.md`.

**Read first, in this order:**

1. **`CLAUDE.md`** — the working agreement (who owns product vs technical
   decisions, and the "surface decisions, not code" reporting style). It governs
   *how* to report; this file covers *what is here*.
2. **`build-foundations.md`** — stack, architecture principles, data model,
   north star. Note §5 predates the implementation; see [Deltas](#deltas-from-build-foundationsmd).
3. **`docs/specs/`** — design specs. Current build: IndieDex MVP.
4. **`docs/OPERATIONS.md`** — deploy, testers, flags. Gitignored (public repo).

---

## The pipeline

```mermaid
flowchart TD
    A["📷 Capture<br/>photo(s) <b>or</b> a clip + GPS"] --> B["IndexedDB queue<br/>(offline-first, owns the bytes)"]
    A2["🖼 Camera roll<br/>a photo taken earlier"] --> A3["POST /photo/metadata<br/>first 128KB only"]
    A3 --> A4{"EXIF has<br/>date + GPS?"}
    A4 -- yes --> A5["geo_source=exif<br/>the file's own date &amp; place"]
    A4 -- no --> A6["ask the person<br/>geo_source=pin or none"]
    A5 --> B
    A6 --> B
    B --> C["POST /sighting<br/>multipart"]

    subgraph SYNC["synchronous — user is waiting"]
        C --> C2{"clip?"}
        C2 -- yes --> C3["extract_diverse_frames<br/>~2 fps · phash-diverse · ≤12<br/>clip never stored"]
        C2 -- no --> D
        C3 --> D["process_photo<br/>EXIF strip · WebP q90 · thumb · phash"]
        D --> E["S3: original + thumb"]
        E --> F["INSERT sighting + photos<br/>(one transaction, orig key only)"]
        F --> G["201 {sighting_id, photo_ids}"]
    end

    G --> H["BackgroundTasks"]

    subgraph BG["background — nothing blocks the response"]
        H --> I["animal_confidence<br/>YOLO26x → sightings.dog_confidence"]
        H --> J["_embed_and_save"]
        J --> K{"best_animal_box<br/>dog or cat?"}
        K -- no --> L["no row written<br/>sighting stays unmatchable"]
        K -- yes --> M["crop +10% margin"]
        M --> N["MiewID-msv3<br/>2152-d, L2-normalised"]
        N --> O[("embeddings.vec_miew<br/>+ bbox + model")]
    end

    O --> P["resolve_sighting<br/>PostGIS radius → HNSW → exact re-rank"]
    P --> R{"top similarity"}
    R -- "≥ auto_merge_min<br/>(1.01 — unreachable by design)" --> S["link to individual"]
    R -- "≥ propose_min" --> T["match_proposals<br/>GET /sighting/{id}/match"]
    R -- else --> U["unmatched"]
    T --> V["POST /proposal/{id}<br/>same | different"]
    V --> W[("individuals + confirmations<br/>the only path that mints an identity")]
    W --> X["GET /dogs<br/>one card per animal · look-alikes ranked, never thresholded"]

    style SYNC fill:#0b3d2e22,stroke:#2d7a5f
    style BG fill:#3d2e0b22,stroke:#7a5f2d
    style L stroke:#b91c1c
    style W stroke:#2d7a5f
```

Verified end to end on real uploads: two sightings of the same dog scored
0.4003, a different dog 716 m away scored 0.1197, one proposal was raised, and a
`same` verdict created the individual and linked both sightings.

**No score merges anything on its own.** `reid_auto_merge_min` is 1.01 —
deliberately unreachable — so every identity comes from a human verdict. The
measurements below say that is not conservatism, it is the only defensible
setting at this data density.

---

## Routes

| route | what it answers |
|---|---|
| `POST /sighting` | a capture: photo(s) or a clip, plus where and when |
| `POST /photo/metadata` | camera-roll preflight — what does this file say about itself? Takes only the first 128KB |
| `GET /dex` | **my** sightings, with full-resolution originals for the gallery |
| `GET /map` | the **cohort's** sightings as pins, thumbnails only, optional `bbox` |
| `GET /dogs` | identified individuals, one card each, with ranked look-alikes |
| `GET /sighting/{id}/match` · `POST /proposal/{id}` | candidates, and the human verdict on them |
| `GET /me` | who the session is, and whether they moderate |
| `POST /sighting/{id}/report` | flag a sighting; it leaves the shared surfaces at once |
| `GET /moderation/queue` · `POST /sighting/{id}/review` | what needs a human, and the ruling |

### review_status finally has a writer

`sightings.review_status` has carried `pending`/`valid`/`rejected` since
migration 0001, and `/map` filtered on it from the day it was written. Nothing
ever set anything but `valid`, so that filter was unreachable code and no path
in the product could take a photo off the shared map.

`POST /sighting/{id}/report` is that writer. One report hides the sighting
(`valid` → `pending`) and a moderator rules on it. Every shared surface now
requires `review_status = 'valid'` — `/map`, `/dogs` and `/proposals` — while
`/dex` still shows you your own whatever its status, and says which status.
Candidate search excludes only `rejected`, so a moderator's takedown cannot
keep seeding identities, while a merely-reported sighting stays matchable.

A moderator's decision is sticky: `reviewed_at` is what stops the next report
quietly overturning it. Reports after a review still surface in the queue, so
re-reporting reaches a human without reaching past one. Moderators are
`observers.trust_tier = 'moderator'`, set by hand.

`/dex` and `/map` differ deliberately: `/dex` means "mine" and is the ownership
semantics `resolve_sighting` reads, while `/map` is cohort-wide and carries
`mine` per sighting so a client filters rather than refetches.

`/map` and `/dogs` show **full precision only for animals you photographed**;
everyone else's collapse to the centre of a ~1 km grid cell (`app/precision.py`,
`settings.map_coarsen_cell_m`). Both apply the identical rule, because the
looser of the two would decide what is actually protected. They used to show
full precision to any signed-in observer, justified by the cohort being
passcode-gated — but the passcode is shared and mints an anonymous observer on
the spot, so anyone holding it could read the exact position of every dog.

A grid cell rather than a jittered point, and that is the whole design: jitter
averages away under repeated fetches, and leaks further the more sightings a dog
has, so the best-documented animals end up the least protected. Snapping is a
function of the input alone, so refreshing and aggregating both reveal nothing.
Still to come from issue #5: a named `area` polygon instead of a cell centre,
plus the delay and marking-suppression dials.

---

## ML subsystem

| Module | Model | Runtime | Cost | Job |
|---|---|---|---|---|
| `app/detect_reid.py` | YOLO26x (ONNX, NMS baked in) | onnxruntime CPU | ~314 ms | animal box + dog/cat confidence |
| `app/embed.py` | MiewID-msv3 (ONNX) | onnxruntime CPU | ~170 ms | 2152-d identity vector |
| `app/detect.py` | YOLOv8n (ONNX) | onnxruntime CPU | ~27 ms | **superseded**, no live callers |

**There is no torch anywhere, and there must not be.** The service runs ONNX
Runtime on CPU. Models are exported on a machine that has torch, via
`scripts/export_miewid_onnx.py` and `scripts/export_yolo26x_onnx.py`.

**The weights are gitignored** (206 MB + 223 MB) and are *not* fetched by the
Dockerfile. Consequences an agent must not trip over:

- A fresh clone has no weights. `_get_session()` raises `ModelUnavailable` /
  `DetectorUnavailable` naming the script to run.
- Model-dependent tests **skip** rather than fail when weights are absent, so a
  green suite does not prove the models work. Check for `skipped` in the output.
- **Deploying as-is silently disables `dog_confidence` in production** — the
  background task catches the error and the sighting still saves. See
  [Before deploying](#before-deploying).

**Licences** (`backend/app/ml/NOTICE.md`): YOLO is AGPL-3.0; **MiewID-msv3
declares no licence at all**. Both are unresolved for a public launch.

---

## Local development

The dev stack is `docker-compose.dev.yml` (Postgres+PostGIS+pgvector, MinIO).

**Every default port collides on some machines**, so the whole local stack is
remappable through untracked overrides. The defaults are unchanged for everyone
else; nothing here is committed. What one machine ended up using, as a worked
example:

| service | default | remapped to | why |
|---|---|---|---|
| Postgres | 5432 | **5433** | another Postgres already bound |
| MinIO API | 9000 | **9002** | port in use |
| MinIO console | 9001 | **9003** | port in use |
| API | 8000 | **8099** | another app already bound |
| Vite (HTTPS, phone) | 5173 | **5174** | 5173 in use |
| Vite (plain HTTP) | — | **5175** | for a headless browser, which will not accept the self-signed cert |

Where each override lives:

- **`docker-compose.dev.local.yml`** — Postgres and MinIO. Compose **appends**
  `ports` lists, so this needs `ports: !override` or the original binding stays
  and still collides.
- **`backend/.env`** — `DATABASE_URL`, `TEST_DATABASE_URL`, `S3_ENDPOINT` must
  all follow the remapped ports, or the app and the tests quietly talk to
  whatever else is on the default port.
- **`frontend/vite.config.local.ts`** — dev server port, HTTPS cert, and the
  proxy target for the moved API. The tracked `vite.config.ts` still points at
  `localhost:8000`.

Run the API on a non-default port with
`uv run uvicorn app.main:app --port 8099`, and remember the frontend proxies to
it server-side, so the port must match in the Vite config too.

Two more things that will waste an hour if you don't know them:

**Storage has two endpoints, on purpose.** `s3_endpoint` is where the server
writes; `s3_public_endpoint` is what presigned URLs are signed against
(defaults to the same). SigV4 signs the host, so a URL signed for the internal
host returns 403 when a browser on a different host fetches it. Needed for
dev-over-LAN and for any CDN in front of S3.

**Tests inherit `backend/.env`.** If it points `S3_PUBLIC_ENDPOINT` at a
self-signed host, `test_storage.py` fails on certificate verification. Run with
`S3_PUBLIC_ENDPOINT= uv run pytest`.

Serving to a phone needs HTTPS: mobile browsers refuse `navigator.geolocation`
outside a secure context, so plain `http://192.168.x.x` yields captures with
`geo_source=none`.

---

## Measured, do not re-litigate

Numbers from a 47,671-frame sweep over 21 encodings (see the `research` repo).
Change these only with new measurements, not intuition.

- **WebP q90 for stored photos.** −0.2 accuracy points vs lossless, 66% smaller
  than the previous JPEG q95 on a real 12 MP capture (674 kB vs 2001 kB).
- **Never re-encode at q100.** On already-compressed sources it *inflated* files
  9.1 kB → 26.3 kB for zero measurable gain.
- **JPEG collapses below ~15 kB; WebP degrades gracefully.** At matched size,
  WebP q30 scored 0.930 against JPEG q10's 0.825.
- **YOLO26x over YOLOv8n for anything re-ID.** On 29 varied photos: dogs 14/17
  vs 9/17, cats 10/12 vs 1/12. The old gate scored a clearly visible dog at
  0.021 where YOLO26x gives 0.800.
- **Preprocessing is load-bearing.** MiewID needs resize-shorter-side-to-440 then
  centre-crop, matching torchvision. Squashing to 440×440 silently shifts every
  embedding away from the distribution the thresholds were measured on.
- **Compression damages calibration before it damages ranking.** Across the
  quality sweep, top-1 barely moved while d′ fell 4.58 → 3.34. Any similarity
  threshold is tied to the encoding it was calibrated on.
- **Re-ID accuracy is governed by how many photos of that dog are already
  stored.** 30 sightings of 10 dogs, session-disjoint, queried through this API:

  | prior photos of the dog | 1 | 2 | 4 | 8 | 32 | 64 | 4,767 |
  |---|---|---|---|---|---|---|---|
  | top-1 | 37% | 43% | 67% | 83% | 87% | 87% | 94.9% |

  The 94.9% in the research repo is a 47,671-frame-gallery number. **A dog with
  one prior photo is a ~40% problem.** Do not quote 94.9% as what a new
  contributor will experience.
- **More photos improve ranking, never separation.** d′ held flat at ~0.85 across
  every gallery size above (genuine ≈0.35, impostor ≈0.245). Proposal precision
  stayed 18–36% at *every* cut-off from 0.20 to 0.50. Collecting more data will
  not rescue a fixed threshold — only a human looking at a ranked candidate will.
- **Presign in batches, not per URL.** `storage.url()` opens an aioboto3 client
  per call. Signing is a local HMAC, but client construction is not: 2.86 ms per
  presign that way against 0.20 ms sharing one. Use `storage.urls()` in any
  route that signs more than a couple of keys — at `/map`'s default limit of
  2000 the difference is ~5.7 s against ~0.4 s of CPU per request.
- **Thumbnails are derived, never stored.** Only the original's key lives on
  `photos`; readers call `photos.thumb_key()`. It is extension-agnostic on
  purpose — the previous `.replace(".jpg", …)` was a silent no-op on `.webp`
  keys, so `thumb_url` served the full-resolution original for months. Measured
  cost of that bug: **8 kB vs 555 kB, 64× per tile.**
- **This pipeline is faithful to the research one.** On identical frames, this
  API (WebP q90 + YOLO26x) scored 50% top-1 against the offline path's 46.7%
  (lossless PNG + YOLOv8n box). Gaps to benchmark numbers are the gallery, not
  the service.

---

## Open questions

**Every threshold number here came from a lab dataset and expires.** The
measurements are all from the Dognosis v2 clips: 10 dogs, one indoor
scent-detection room, constant background and lighting, fixed cameras, animals
in harnesses, and a population skewed to a few breeds — which is exactly why
look-alikes dominate it. Street sightings vary in background, light, distance
and angle, and free-roaming indies vary far more in coat and size than a kennel
of beagles, while the population is much larger. Those pull in opposite
directions and neither is measured.

**Recalibrate weekly as real verdicts arrive.** `confirmations` gives the label
and `match_proposals.score` gives what the model said; fitting the threshold is
a query over those two, not a new experiment. Treat `reid_propose_min` as a
placeholder until it has been fitted at least once on real data.

**Match thresholds are unset, deliberately**, and the measurements above suggest
a *threshold* is the wrong control surface. At d′ ≈ 0.85 no cut-off separates
same-dog from different-dog: 0.25 gave 18% proposal precision, and 0.50 gave 36%
while losing 71% of true matches. `propose_min` currently admits up to 5
candidates per sighting, which on this data is ~4.8 proposals per upload — review
fatigue for almost no signal.

Proposing the **top-1 candidate only**, regardless of absolute score, is the
change worth making: one question per sighting instead of five, at similar
precision. That is a product call about how much reviewer time a sighting is
worth, so it is Akash's. Fit any number to real verdicts in `confirmations`; do
not port numbers from the research repo.

**Ask for more frames when a match is ambiguous.** The single largest lever
measured is frames per individual — 1 → 8 frames takes top-1 from 37% to 83%,
far more than any threshold or encoding change. This is the strongest argument
for the deferred video capture, and for prompting a contributor for a second
angle when the top candidates cluster.

**Cats.** `detect_reid` embeds dogs *and* cats (COCO 15+16). Whether cats are in
scope is a product decision.

**Video capture is now in** — PR #3's two commits cherry-picked, its capture
screen rebuilt on the current multi-photo component. **Do not try to merge
`feat/video-capture`**: it predates the history rewrite and shares no common
ancestor with `main`.

Frame selection still uses phash, which compares whole scenes rather than the
animal; embedding-space diversity would be better now that the embedder is
in-tree, and sampling the middle 80% of a clip would help (detection is 93%
mid-clip vs ~39% at the edges).

---

## The connection pool is shared with the background tasks

`asyncpg.create_pool` defaults to `max_size=10`, and this pool has two very
different consumers: request handlers, which hold a connection for the whole
request through the `get_conn` dependency, and the background tasks that embed
and match after the response. Concurrent uploads exhausted it.

Measured: 16 uploads at once, **exactly 10 served and 6 never handed to the app
at all** — they sat suspended on `pool.acquire()` until the client gave up at
90 s. With `db_pool_max = 30`, all 16 return 201 in ~1.7 s.

The reason this took so long to find: **a suspended coroutine has no thread
stack**, so `faulthandler` dumps showed only idle worker threads and no
application frames. Every observation said "the server is idle" while six
requests hung, which reads like a deadlock and is not one. If something similar
appears again, count the served requests first — a number that matches a pool or
limiter size is the whole answer.

`backend/sitecustomize.py` (untracked, local) registers a SIGUSR1 stack dump:
`kill -USR1 <uvicorn pid>`. Start uvicorn with `PYTHONPATH=.` or it is never
imported — a console script puts `.venv/bin` on `sys.path`, not the cwd.

---

## Before deploying

**`main` deploys straight to production.** `.github/workflows/deploy.yml` fires
on pushes to `main` that can affect the running app, SSHes to the box,
`git pull` + `docker compose up --build`, then checks the site returns 200 —
which proves the app boots, not that it works. `/health` also reports whether
re-ID is `ready` or `degraded`, which is the falsifiable part.

There **is** a staging box (`deploy-staging.yml`, manual dispatch). Use it for
anything touching the ML path; production is one merge away and has no
intermediate step.

`ci.yml` runs pytest, vitest and tsc on pull requests and pushes to `main`, but
it is **not a required gate**: nothing stops a red merge from deploying. Wiring
it into branch protection is a repo-settings decision.

So merging is deploying. Before any merge touching the ML path:

- [ ] **Model weights reach the box.** Run the **Seed model weights** workflow
      (Actions → run manually, pick the environment). It exports both ONNX on a
      runner, ships them over SSH, and has the box upload them to object storage
      using its own credentials — so nothing is downloaded to a laptop and no S3
      secret is added to GitHub. Once done, `entrypoint.sh` fetches them on every
      future boot and this is never needed again.

      Until then `/health` reports `"reid": "degraded"` and every upload saves
      with no embedding: re-ID looks deployed and does nothing.
- [ ] Box RAM fits ~430 MB of resident model weights plus runtime.
- [ ] **Backfill existing photos** — `uv run python scripts/backfill_embeddings.py
      --resolve` from `/app/backend`. Nothing embeds them automatically, and
      `find_candidates` filters on `vec_miew IS NOT NULL`, so until this runs the
      whole existing corpus is invisible to matching. Given that accuracy tracks
      gallery density, skipping it starts the system at its worst point.
- [ ] Licence position settled for anything shipped in a public artifact.
- [ ] `S3_PUBLIC_ENDPOINT` correct for the environment.
- [ ] Note that `dog_confidence` is not comparable across this deploy: existing
      rows were scored by YOLOv8n, new ones by YOLO26x.

---

## Deltas from `build-foundations.md`

§5 was written before implementation and no longer matches:

| §5 says | Actually |
|---|---|
| MegaDescriptor / DINOv2 | **MiewID-msv3**, 2152-d |
| `sightings.embedding` column | **`embeddings` table**, one row per (photo, model) |
| `<=>` on the vector column | `vec_miew::halfvec(2152)` — pgvector HNSW caps at 2000 dims, MiewID is 2152 |
| geo prior ~300 m | `find_candidates` defaults to a caller-supplied radius; 2 km used in testing |
| species filter | not implemented; `detect_reid` returns the COCO class but it is not persisted |

Treat this table as the current truth and §5 as intent.
