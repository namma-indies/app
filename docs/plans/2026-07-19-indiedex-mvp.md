# IndieDex MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship an installable mobile PWA where trusted, separately-authed users photograph street dogs (auto GPS + time) and browse their captures on a map + gallery, backed by the full re-ID-ready v2 schema — no ML, no grouping.

**Architecture:** FastAPI (async) on a single Lightsail VM serving a built React PWA and a JSON API (`POST /sighting`, `GET /dex`) behind a pluggable magic-link auth seam. Postgres 16 + PostGIS + pgvector holds the full v2 schema day one; the MVP writes only `observers`/`sightings`/`photos` and leaves the re-ID tables empty. Photos live in S3 (key in DB). Most of the build runs locally against Docker; only Phase H needs the provisioned box.

**Tech Stack:** Python 3.12 · FastAPI · uvicorn · asyncpg (raw SQL for spatial/vector) · Alembic (migration versioning) · PostGIS · pgvector · aioboto3 (S3) · Pillow + imagehash · uuid-utils (UUIDv7) · itsdangerous (signed tokens/cookies) · React + Vite + TypeScript · MapLibre GL · vite-plugin-pwa · pytest + httpx · uv (deps) · Caddy (auto-TLS) · systemd.

## Global Constraints

- **UUIDv7 for every PK** — generated app-side via `uuid-utils` (Postgres 16 has no native `uuidv7()`); never expose sequential IDs.
- **`phone_hash` = HMAC-SHA256 with a server-side secret**, never raw numbers, never plain SHA-256.
- **Enums = `text` + `CHECK`**, never native Postgres enums. `updated_at` on every mutable table.
- **Two Postgres roles** from the first migration: `app_rw` (application) and `public_read` (granted only future aggregate views; must not be able to see `individual_id` or raw `geog`).
- **Full v2 migration ships in Phase B** — all 10 tables (`observers`, `sightings`, `photos`, `embeddings`, `individuals`, `match_proposals`, `confirmations`, `clinical_records`, `areas`, `jobs`), even though only 3 are written.
- **Secure context required** — camera + `navigator.geolocation` only work over HTTPS; local dev uses `localhost` (a secure origin), production uses Caddy auto-TLS.
- **Auth behind an `AuthProvider` seam** — magic-link is one provider; sessions are provider-agnostic so Google/Facebook OAuth add later with no change to sessions, the observer model, or app code.
- **Per-observer data isolation** — every write binds to the session's `observer_id`; `GET /dex` returns only the caller's sightings.
- Code license MIT; captured data is restricted (no public endpoints exist in this MVP).

---

## Phases & the provisioning gate

| Phase | Deliverable | Needs the Lightsail box? |
|---|---|---|
| A | Local foundation: repo scaffold, dev DB, config, UUIDv7 | No |
| B | Full v2 migration + roles, verified | No |
| C | Storage seam (S3 / MinIO) | No |
| D | Pluggable auth (magic-link) + sessions | No |
| E | `POST /sighting` ingest (photo, geo, time, HMAC observer) | No |
| F | `GET /dex` per-observer read | No |
| G | React PWA: capture + IndieDex (map + gallery) + offline | No |
| H | Provision Lightsail + deploy (Caddy, systemd, on-box PG, S3, backups) | **Yes — blocked on box** |

Phases A–G are fully buildable and testable **now**, before the box exists. Phase H is the only one gated on Akash's Lightsail setup + SSH access.

---

## File structure

```
backend/
  pyproject.toml                 # uv-managed deps
  app/
    config.py                    # settings from env (secrets, DB url, S3)
    ids.py                       # uuid7() helper
    db.py                        # asyncpg pool
    security.py                  # phone_hash HMAC, cookie signer
    storage/
      base.py                    # Storage protocol
      s3.py                      # S3/MinIO impl
    auth/
      base.py                    # AuthProvider protocol, Session model
      magiclink.py               # magic-link provider
      session.py                 # provider-agnostic session cookie
      deps.py                    # FastAPI dependency: require_observer
    photos.py                    # EXIF strip, dimensions, phash
    routes/
      sighting.py                # POST /sighting
      dex.py                     # GET /dex
      auth.py                    # POST /auth/magic-link/consume etc.
    main.py                      # app wiring, static PWA mount
  migrations/                    # Alembic; 0001 = full v2 schema
  tests/
    conftest.py                  # test DB + client fixtures
    test_*.py
frontend/
  package.json  vite.config.ts  # React + TS + vite-plugin-pwa
  src/
    api.ts                       # typed fetch to /sighting, /dex
    auth.ts                      # session bootstrap
    screens/Capture.tsx
    screens/Dex.tsx              # map + gallery
    components/DogMap.tsx        # MapLibre wrapper
    offline/queue.ts             # IndexedDB capture queue + sync
    main.tsx  sw.ts
Dockerfile                       # multi-stage: build frontend -> install backend -> serve both
docker-compose.dev.yml           # local: postgis+pgvector, minio
docker-compose.prod.yml          # on VM: caddy + app + (postgres | RDS via env)
deploy/
  Caddyfile                      # reverse proxy + auto-TLS (containerized)
  provision.md                   # box bootstrap runbook (install docker, pull, up)
  backup.sh                      # pg_dump -> S3
```

---

## Phase A — Local foundation

### Task A1: Repo scaffold + dev database + UUIDv7

**Files:**
- Create: `backend/pyproject.toml`, `backend/app/__init__.py`, `backend/app/ids.py`, `docker-compose.dev.yml`
- Create: `backend/tests/test_ids.py`

**Interfaces:**
- Produces: `app.ids.uuid7() -> uuid.UUID` (time-ordered v7).

**Decisions locked here:** deps via `uv`; dev Postgres is a Docker image carrying **both** PostGIS and pgvector (`docker-compose.dev.yml` builds from `postgis/postgis:16-3.4` and `CREATE EXTENSION vector`); MinIO container for S3-compatible local storage.

- [ ] **Step 1: Write the failing test**
```python
# backend/tests/test_ids.py
from app.ids import uuid7

def test_uuid7_is_version_7_and_time_ordered():
    a = uuid7(); b = uuid7()
    assert a.version == 7
    assert a < b  # v7 is monotonic by time
```
- [ ] **Step 2: Run it, verify it fails** — `cd backend && uv run pytest tests/test_ids.py -v` → FAIL (module missing).
- [ ] **Step 3: Implement**
```python
# backend/app/ids.py
import uuid_utils
import uuid

def uuid7() -> uuid.UUID:
    return uuid.UUID(str(uuid_utils.uuid7()))
```
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Write `docker-compose.dev.yml`** with two services: `db` (PostGIS+pgvector, expose 5432, POSTGRES_PASSWORD dev) and `minio` (expose 9000/9001). Bring up: `docker compose -f docker-compose.dev.yml up -d`. Verify: `docker compose exec db psql -U postgres -c "CREATE EXTENSION IF NOT EXISTS postgis; CREATE EXTENSION IF NOT EXISTS vector; SELECT postgis_version();"` succeeds.
- [ ] **Step 6: Commit** — `feat: repo scaffold, dev db (postgis+pgvector+minio), uuid7`.

---

## Phase B — Migration & data layer

### Task B1: Full v2 migration + two roles

**Files:**
- Create: `backend/migrations/versions/0001_full_v2_schema.py` (Alembic revision executing raw SQL), `backend/alembic.ini`, `backend/migrations/env.py`
- Create: `backend/tests/test_migration.py`

**Interfaces:**
- Produces: all 10 tables + indexes; roles `app_rw`, `public_read`.

**Schema authority:** column set follows `docs/specs/2026-07-10-design.md` §2.2 + `docs/specs/2026-07-19-indiedex-mvp-design.md` §3. Key points the migration MUST encode:
- `sightings.geog geography(Point,4326)`; GIST index on `geog`; `individual_id uuid NULL`; `match_status text NOT NULL DEFAULT 'unmatched' CHECK (match_status IN ('unmatched','proposed','confirmed'))`; `review_status text NOT NULL DEFAULT 'valid' CHECK (...)`; `geo_source text CHECK (geo_source IN ('device_gps','pin','none'))`; `geo_accuracy_m double precision NULL`; `captured_at`/`reported_at timestamptz`; `phash text NULL`; `attrs jsonb NOT NULL DEFAULT '{}'`; `created_at`/`updated_at`.
- `embeddings.vec vector` **untyped** (per D4 — no typed dim, no HNSW yet) + `model text`, `dim int`, `bbox jsonb NULL`, `UNIQUE(photo_id, model)`.
- `observers.phone_hash text UNIQUE NULL`, `contact_enc bytea NULL`, `trust_tier text`, `deleted_at timestamptz NULL`.
- Roles: `CREATE ROLE app_rw`; `CREATE ROLE public_read`; grant `app_rw` DML on all tables; grant `public_read` **nothing yet** (aggregate views are a future slice).

- [ ] **Step 1: Write failing test**
```python
# backend/tests/test_migration.py
import asyncpg, pytest

TABLES = {"observers","sightings","photos","embeddings","individuals",
          "match_proposals","confirmations","clinical_records","areas","jobs"}

@pytest.mark.asyncio
async def test_all_tables_exist(migrated_db: asyncpg.Connection):
    rows = await migrated_db.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname='public'")
    assert TABLES.issubset({r['tablename'] for r in rows})

@pytest.mark.asyncio
async def test_public_read_cannot_see_individual_id(migrated_db):
    # public_read has no grants on sightings
    has = await migrated_db.fetchval(
        "SELECT has_table_privilege('public_read','sightings','SELECT')")
    assert has is False

@pytest.mark.asyncio
async def test_sightings_geog_is_gist_indexed(migrated_db):
    idx = await migrated_db.fetch(
        "SELECT indexdef FROM pg_indexes WHERE tablename='sightings'")
    assert any('gist' in r['indexdef'].lower() and 'geog' in r['indexdef'].lower() for r in idx)
```
- [ ] **Step 2: Run, verify fail** (no migration yet).
- [ ] **Step 3: Write the Alembic revision** executing the full raw-SQL schema above (CREATE EXTENSION postgis, vector; all tables with CHECK-based enums; GIST on `geog`; `UNIQUE(photo_id, model)`; the two roles + grants).
- [ ] **Step 4: Apply + run tests** — `uv run alembic upgrade head && uv run pytest tests/test_migration.py -v` → PASS.
- [ ] **Step 5: Commit** — `feat: full v2 migration + app_rw/public_read roles`.

### Task B2: asyncpg pool + config

**Files:** Create `backend/app/config.py`, `backend/app/db.py`, `backend/tests/conftest.py` (fixtures: `migrated_db`, `app_client`).

**Interfaces:**
- Produces: `app.config.settings` (DB URL, `PHONE_HASH_SECRET`, `SESSION_SECRET`, `MAGIC_LINK_SECRET`, S3 creds/bucket/endpoint); `app.db.pool()` → asyncpg pool; `app.db.acquire()` dependency.

- [ ] **Step 1–5:** Standard TDD — test that `settings` loads from env and `pool()` round-trips `SELECT 1`. Commit `feat: config + asyncpg pool + test fixtures`.

---

## Phase C — Storage seam

### Task C1: Storage protocol + S3/MinIO impl

**Files:** Create `backend/app/storage/base.py`, `backend/app/storage/s3.py`, `backend/tests/test_storage.py`.

**Interfaces:**
- Produces:
```python
class Storage(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...
    async def url(self, key: str, expires_s: int = 3600) -> str: ...   # presigned GET
```
- `S3Storage(endpoint, bucket, creds)` implements it via aioboto3; works against MinIO in dev, S3 in prod (same code).

- [ ] **Steps:** Test round-trips bytes to MinIO and returns a presigned URL that fetches them back (integration test against the compose MinIO). Commit `feat: storage seam (S3/MinIO)`.

---

## Phase D — Pluggable auth

### Task D1: Session cookie (provider-agnostic) + `phone_hash`

**Files:** Create `backend/app/security.py`, `backend/app/auth/base.py`, `backend/app/auth/session.py`, `backend/app/auth/deps.py`, `backend/tests/test_security.py`, `backend/tests/test_session.py`.

**Interfaces:**
- Produces:
```python
def phone_hash(e164: str) -> str        # HMAC-SHA256 hex, keyed by settings.PHONE_HASH_SECRET
def issue_session(observer_id: UUID) -> str   # signed cookie value (itsdangerous)
def read_session(cookie: str) -> UUID | None
# FastAPI dependency:
async def require_observer(request) -> UUID    # 401 if no/invalid session
# auth/base.py:
class AuthProvider(Protocol):
    async def resolve(self, ...) -> UUID: ...   # -> observer_id (creating observer if needed)
```

- [ ] **Step 1: Failing tests**
```python
def test_phone_hash_is_hmac_not_plain_sha256():
    from app.security import phone_hash
    import hashlib
    h = phone_hash("+919999999999")
    assert h != hashlib.sha256(b"+919999999999").hexdigest()
    assert phone_hash("+919999999999") == h  # deterministic

def test_session_roundtrip_and_tamper():
    from app.security import issue_session, read_session
    from app.ids import uuid7
    oid = uuid7()
    tok = issue_session(oid)
    assert read_session(tok) == oid
    assert read_session(tok[:-2] + "xx") is None
```
- [ ] **Steps 2–5:** implement HMAC (`hmac.new(secret, e164, sha256)`) and itsdangerous `URLSafeSerializer`; `require_observer` reads the cookie, 401s on miss. Commit `feat: HMAC phone_hash + provider-agnostic session + require_observer`.

### Task D2: Magic-link provider

**Files:** Create `backend/app/auth/magiclink.py`, `backend/app/routes/auth.py`, `backend/tests/test_magiclink.py`.

**Interfaces:**
- Produces:
  - `mint_link(observer_id) -> str` (signed, expiring token URL) — an operator tool to admit a tester.
  - `POST /auth/magic-link/consume?token=…` → validates token, upserts the `observer`, sets the session cookie, redirects to `/`.
- Consumes: `issue_session`, `phone_hash`, the pool.

- [ ] **Steps:** Test that a minted token consumes once → sets a valid session; an expired/garbage token → 401. Provide a tiny CLI (`python -m app.auth.magiclink mint <label>`) to print a link. Commit `feat: magic-link auth provider + consume route + mint CLI`.

**Note (decision):** magic-link tokens are the admission mechanism; there is no self-serve signup. Social login later = a new `AuthProvider` implementing `resolve`, mounted at its own route, reusing `issue_session` unchanged.

---

## Phase E — Ingest

### Task E1: Photo processing (strip metadata, dimensions, phash)

> **PENDING [issue #1](https://github.com/namma-indies/app/issues/1) (Aswin):** final format/fidelity + the video→frames path. Build the **conservative default below** so his answer is a config/derivative change, not a rewrite.

**Files:** Create `backend/app/photos.py`, `backend/tests/test_photos.py`.

**Design stance (fidelity-preserving, integrates issue #1):**
- **Persist a high-fidelity, metadata-stripped original** — strip EXIF/GPS **without gratuitous recompression** (prefer lossless metadata removal; if re-encoding, high quality, no downscale). The original is the ML-grade asset for future re-ID; do not degrade it.
- **Derive a separate small thumbnail** for the gallery, stored under its own key — the app reads thumbnails, models read originals.
- **`process_photo` returns both**, so if Aswin wants a different original policy it's one function's internals; storage layout and callers don't change.
- **Video path is pre-accommodated, not built:** schema is 1 sighting → N photos, so a future short-video ingest = server-side frame extraction producing N `ProcessedPhoto`s under one sighting. No new tables.

**Interfaces:**
```python
@dataclass
class ProcessedPhoto:
    original: bytes; thumbnail: bytes; width: int; height: int; phash: str; content_type: str
def process_photo(raw: bytes) -> ProcessedPhoto   # metadata-stripped original (fidelity kept) + thumbnail + imagehash.phash
```

- [ ] **Steps:** Test output original has **no EXIF** (`Pillow getexif()` empty) yet dimensions equal the input (no silent downscale of the original); a thumbnail is produced and is smaller; `phash` is stable for the same image and differs for another. Commit `feat: photo processing (metadata strip, fidelity-preserving original + thumbnail + phash)`.

### Task E2: `POST /sighting`

**Files:** Create `backend/app/routes/sighting.py`, wire in `backend/app/main.py`, `backend/tests/test_sighting.py`.

**Interfaces:**
- Consumes: `require_observer`, `Storage`, `process_photo`, `uuid7`, pool.
- Produces: `POST /sighting` — multipart: `photos[]` (≥1), `lat`, `lng`, `geo_accuracy_m?`, `geo_source`, `captured_at`, `reported_at?`, `note?`. Creates one `sighting` (individual_id NULL, match_status 'unmatched', review_status 'valid') + N `photos`; stores **original + thumbnail** to `Storage` under `sightings/{sighting_id}/{photo_id}.jpg` and `.../{photo_id}_thumb.jpg` (`photos.s3_key` = original; thumbnail key is derived). Returns `{sighting_id, photo_ids}`.

- [ ] **Step 1: Failing tests**
```python
@pytest.mark.asyncio
async def test_post_sighting_requires_auth(app_client):
    r = await app_client.post("/sighting", files=..., data=...)
    assert r.status_code == 401

@pytest.mark.asyncio
async def test_post_sighting_creates_rows_and_stores_photo(authed_client, db):
    r = await authed_client.post("/sighting",
        files=[("photos", ("d.jpg", JPEG_BYTES, "image/jpeg"))],
        data={"lat":"12.97","lng":"77.59","geo_source":"device_gps",
              "geo_accuracy_m":"8.0","captured_at":"2026-07-19T10:00:00Z"})
    assert r.status_code == 201
    sid = r.json()["sighting_id"]
    row = await db.fetchrow("SELECT individual_id, match_status, review_status, "
                            "ST_Y(geog::geometry) AS lat FROM sightings WHERE id=$1", sid)
    assert row["individual_id"] is None
    assert row["match_status"] == "unmatched" and row["review_status"] == "valid"
    assert abs(row["lat"] - 12.97) < 1e-6
    n = await db.fetchval("SELECT count(*) FROM photos WHERE sighting_id=$1", sid)
    assert n == 1

@pytest.mark.asyncio
async def test_geo_denied_stores_geo_source_none(authed_client, db):
    r = await authed_client.post("/sighting", files=[...],
        data={"geo_source":"none","captured_at":"2026-07-19T10:00:00Z"})
    assert r.status_code == 201  # capture never blocked on GPS
```
- [ ] **Steps 2–5:** implement insert (`ST_SetSRID(ST_MakePoint(lng,lat),4326)::geography`), store photos, bind `observer_id` from session. Commit `feat: POST /sighting ingest`.

---

## Phase F — Dex read

### Task F1: `GET /dex`

**Files:** Create `backend/app/routes/dex.py`, wire in `main.py`, `backend/tests/test_dex.py`.

**Interfaces:**
- Consumes: `require_observer`, `Storage`, pool.
- Produces: `GET /dex` → `{sightings: [{id, captured_at, lat, lng, geo_accuracy_m, photos:[{url}]}]}` — **only the caller's** sightings, reverse-chron, photo URLs presigned.

- [ ] **Step 1: Failing test**
```python
@pytest.mark.asyncio
async def test_dex_returns_only_own_sightings(app_client, db, two_observers):
    a, b = two_observers
    # a captures 2, b captures 1
    ...
    r = await client_for(a).get("/dex")
    ids = [s["id"] for s in r.json()["sightings"]]
    assert len(ids) == 2  # b's sighting absent
    assert r.json()["sightings"][0]["photos"][0]["url"].startswith("http")
```
- [ ] **Steps 2–5:** implement query filtered by `observer_id`, join photos, presign URLs. Commit `feat: GET /dex per-observer read`.

---

## Phase G — React PWA

> Frontend tasks specify files, interfaces, and **acceptance criteria** rather than full component source: exact MapLibre/React/vite-plugin-pwa code is verified interactively (Playwright MCP + dev server) during execution, since blind full-code for these libraries would be rewritten on first run. Each task still ends with a concrete, observable check.

### Task G1: PWA scaffold + typed API client + session bootstrap

**Files:** Create `frontend/package.json`, `frontend/vite.config.ts` (with `vite-plugin-pwa`, manifest: name "IndieDex", standalone display, icons), `frontend/src/api.ts`, `frontend/src/auth.ts`, `frontend/src/main.tsx`.

**Interfaces (`api.ts`):**
```ts
export type Sighting = { id:string; captured_at:string; lat:number|null; lng:number|null;
                         geo_accuracy_m:number|null; photos:{url:string}[] };
export async function postSighting(input: {...}): Promise<{sighting_id:string}>;
export async function getDex(): Promise<{sightings: Sighting[]}>;
```
- [ ] **Acceptance:** `npm run dev` serves on `localhost` (secure origin); app loads, calls `/dex` (401 when no session → shows a "you need a link" state). Chrome DevTools → Application shows an installable manifest + registered service worker. Commit `feat: PWA scaffold + api client + auth bootstrap`.

### Task G2: Capture screen

**Files:** Create `frontend/src/screens/Capture.tsx`, `frontend/src/offline/queue.ts`.

**Behavior:** camera capture (`getUserMedia`/`<input capture>`); on shot, read `navigator.geolocation` (lat/lng/accuracy) with a timeout; optional note; submit via `postSighting`. GPS denied/timeout → submit with `geo_source:'none'`. Offline → enqueue to IndexedDB (`offline/queue.ts`) and flush on reconnect.

- [ ] **Acceptance (Playwright MCP, mocked geolocation + a fake camera image):** a capture with granted geo posts a sighting that appears in the DB with `device_gps`; with geo denied, still posts `none`; with the network offline then online, the queued capture syncs. Commit `feat: capture screen + offline queue`.

### Task G3: IndieDex screen (map + gallery)

**Files:** Create `frontend/src/screens/Dex.tsx`, `frontend/src/components/DogMap.tsx`.

**Behavior:** `getDex()` → MapLibre map (OSM raster source behind a single style-URL swap — **PENDING [issue #2](https://github.com/namma-indies/app/issues/2) (Aswin) for the real provider**; OSM raster is the MVP placeholder) with a clustered pin per sighting; tap pin → photo + time + accuracy popup. Below/toggle: reverse-chron gallery grid; tap card → detail. Sightings with null geo render in the gallery only, flagged "no location."

- [ ] **Acceptance (Playwright MCP):** after seeding 3 sightings for the authed observer, the map shows 3 pins (or a cluster) and the gallery shows 3 cards; another observer's sightings never appear. Commit `feat: IndieDex map + gallery`.

---

## Phase H — Containerize & deploy (GATED: needs Lightsail box + SSH)

> **Docker-first (Akash's call):** the app ships as a container image; the VM just runs `docker compose up`. Do not start until Akash has created the Lightsail Ubuntu VM + AWS S3 bucket and granted CLI/SSH. Verification is end-to-end from a phone.

### Task H1: Dockerfile + prod compose (buildable now, no box)

**Files:** Create `Dockerfile`, `docker-compose.prod.yml`.
- [ ] Multi-stage `Dockerfile`: stage 1 builds the frontend (`npm run build`); stage 2 installs the backend (uv) and copies the built static assets; runs uvicorn serving both API and PWA. `docker-compose.prod.yml` wires three services — `caddy` (auto-TLS, reverse-proxy), `app` (the image), `db` (postgis+pgvector; **omit-able when `DATABASE_URL` points at RDS**). **Acceptance (local):** `docker compose -f docker-compose.prod.yml up` on the laptop serves the app on `localhost`, `/dex` → 401. *(This task needs no box — it's containerization, so it runs in the main build, not gated.)*

### Task H2: Image registry + box bootstrap

**Files:** Create `deploy/provision.md`.
- [ ] Publish the image to **GHCR** (`ghcr.io/namma-indies/app`); on the VM install Docker + compose, log in to GHCR, pull. **DB host is a connection-string swap:** on-box `db` container for the pilot (per spec's single-VM principle), **RDS the likely production move** (RDS Postgres supports `postgis`+`vector`) — set via `DATABASE_URL`, no rework. **Acceptance:** `docker compose -f docker-compose.prod.yml pull` succeeds on the box; migrations run (`docker compose run app alembic upgrade head`); `psql` shows all 10 tables + `app_rw`/`public_read`.

### Task H3: S3 bucket + secrets + TLS domain

**Files:** update `deploy/provision.md`; `deploy/Caddyfile`.
- [ ] Create the S3 bucket (`ap-south-1`) + a scoped IAM user; put secrets (session/HMAC/magic-link keys, DB url, S3 creds) in a `.env` on the box consumed by compose; point a domain/subdomain at the VM so Caddy auto-provisions TLS. **Acceptance:** from a phone over HTTPS, mint a magic link, log in, photograph a real dog, see it in the IndieDex map — the full loop live. Satisfies spec §7 acceptance criteria 1–8.

### Task H4: Backups

**Files:** Create `deploy/backup.sh`.
- [ ] Cron `pg_dump` (via `docker compose exec db`) → S3 daily; enable Lightsail auto-snapshots. **Acceptance:** a dump object lands in S3; a restore into a scratch DB reproduces the schema + rows.

---

## Self-review notes

- **Spec coverage:** §1 scope → Phases A–H; §2 architecture/stack → A,B,H; §3 data model + re-ID seam → B1 (full migration, empty tables); §4 capture → E,G2; §4 IndieDex → F,G3; §5 pluggable auth → D; §6 guardrails → B1 (roles, uuidv7, HMAC), D1 (HMAC); §7 acceptance → mapped in H3 + per-task acceptance; §8 upgrade path → preserved by B1's full schema.
- **Provisioning gate** isolates the only box-dependent work (Phase H) so A–G proceed during Lightsail setup.
- **Frontend code deliberately specified as acceptance-driven** (not full blind source) — noted at Phase G; every such task still ends in an observable check.
