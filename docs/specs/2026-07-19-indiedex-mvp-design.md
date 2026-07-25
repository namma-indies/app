# IndieDex MVP — design spec

**Date:** 2026-07-19
**Status:** Draft for review
**Companion docs:** `build-foundations.md` (stack, guardrails, north star), `docs/specs/2026-07-10-design.md` (full v1 architecture + eight slices). This doc carries stack and guardrails from those by reference and does not re-derive them.

This spec defines the **first buildable thing**: a tool Akash can walk around Bangalore with, photograph street indies, and browse them on a map — the **IndieDex**. It is the full-v1 spec's **Slice 1 + Slice 2 plus a new internal viewing surface**, and nothing else.

---

## 1. What this MVP is (and is not)

**Is:** one authed mobile web app (installable PWA) on a Lightsail box with two screens —
- **Capture** — snap a street dog → auto GPS + time → stored as a sighting.
- **IndieDex** — a map of pins + a scrollable gallery of every sighting you've collected.

Every capture is a `sighting` with `individual_id = NULL` — the "empty slot" from `build-foundations.md` §1. The MVP never fills it.

**Multi-user from day one.** Akash plus a few trusted testers each get a separate `observer` identity and log in independently — this replicates the real crowdsourced flow (many distinct contributors) rather than a single-user prototype. Each user's IndieDex shows **their own** captures (per-observer); a shared/collective view is a later product decision, not built now.

**Is NOT (deferred by design):**
- No re-ID / ML — no embeddings, no matching, no candidate retrieval.
- No individuals layer — no "same dog" grouping, manual or automatic.
- No WhatsApp intake, no public surface, no heatmap, no population estimate.
- No clinical records, no feeder confirmation loop.

**The core bet (open-set re-ID) is explicitly out of scope for this build.** This is deliberate: get a scalable capture-and-view loop into your hands first; the ML lights up later against real data with zero rebuild.

---

## 2. Architecture

Approach: **spec-faithful Lightsail** (the full v1 stack, stood up now — not a managed-host shortcut). Akash provisions the AWS account + Lightsail VM and grants CLI/SSH; the app + services are provisioned onto it.

```
   Phone (Chrome/Safari, HTTPS) — installable PWA
        │  camera + navigator.geolocation
        ▼
   ┌─────────────────────── Lightsail VM (Ubuntu) ───────────────────────┐
   │  Caddy (TLS, auto-cert)  ──▶  FastAPI (async, uvicorn)               │
   │                                ├─ POST /sighting  (photo + geo + time)│
   │                                ├─ GET  /dex       (your sightings)    │
   │                                ├─ auth (magic-link session cookie)    │
   │                                └─ serves the built React PWA (static) │
   │                                         │                             │
   │                                Postgres 16 + PostGIS + pgvector       │
   │                                (full v2 migration; re-ID tables empty)│
   └─────────────────────────────────────────┬───────────────────────────┘
                                              │ photo bytes (key in DB)
                                              ▼
                                     S3 bucket (ap-south-1)
```

**Stack (from `build-foundations.md` §3, unchanged):** Lightsail Ubuntu VM · PostgreSQL 16 + PostGIS + pgvector · Python + FastAPI · S3 for photos (key in DB, never blobs). TLS via Caddy (auto HTTPS — required because camera + `navigator.geolocation` only work in a secure context).

**Frontend:** React + Vite + MapLibre GL, shipped as an installable **PWA** (`vite-plugin-pwa`). Built to static assets, served by the same box. Chosen so UI polish is incremental (never a rewrite) and the installed PWA doubles as the "mobile app" for the foreseeable pilot; a future native app (Expo/React Native) reuses the React model and is just another API client.

**The swappable-client principle (`docs/specs/2026-07-10-design.md` §2.1b):** `POST /sighting` is a stable JSON contract. The PWA is its first client. A nicer UI, a native app, or a WhatsApp channel later are all *additional clients of the same API* — the backend, schema, and storage never rebuild.

---

## 3. Data model (what's live now vs. waiting)

The **full v2 migration ships day one** (all 10 tables from `docs/specs/2026-07-10-design.md` §2.2). The MVP writes only three; the rest exist empty as the re-ID seam.

### Written now

| Table | Role | Key columns used |
|---|---|---|
| `observers` | who captured it | `id` (uuidv7), `phone_hash` (HMAC), `display_name`, `trust_tier`, `created_at`, `deleted_at` |
| `sightings` | one row per capture — **the entry** | `id`, `observer_id`, `captured_at`, `reported_at`, `geog` (POINT, PostGIS), `geo_source`, `geo_accuracy_m`, **`individual_id` NULL**, `match_status='unmatched'`, `review_status='valid'`, `phash`, `attrs` (jsonb), `created_at`, `updated_at` |
| `photos` | 1→n photos per sighting | `id`, `sighting_id`, `s3_key`, `width`, `height`, `phash`, `created_at` |

### Exists, empty, waiting (the re-ID seam)

`individuals`, `embeddings`, `match_proposals`, `confirmations`, `clinical_records`, `areas`, `jobs`.

**Why this is the whole point:** enabling re-ID later means (a) add an embed worker reading `photos` → writing `embeddings`, (b) add the geo-prefilter → vector-rank query writing `match_proposals`, (c) promote `sightings.individual_id` NULL → set. **No schema change, no data migration, no touch to the capture app or the IndieDex.** The empty slot is present from sighting #1.

### Small calls made here
- **`geo_accuracy_m` stored per capture** — browser geolocation returns an accuracy radius; keeping it lets later re-ID weight which pins to trust. Free now.
- **`phash` computed on upload** — cheap dedup + the forwarded-image tripwire from `docs/specs/2026-07-10-design.md` D7. Costs nothing now, useful later.

---

## 4. The two screens

### Screen 1 — Capture (default view, one-thumb flow)
1. Camera button → native camera (`<input capture>` / `getUserMedia`) → one or more photos.
2. On capture, silently read `navigator.geolocation` (lat/lng + accuracy) and the timestamp.
3. Optional minimal `attrs`: a free-text note (quick chips like collar?/notch? may come later); stored in `attrs` jsonb.
4. Submit → `POST /sighting` (multipart: photo bytes + geo + time). A **server-side dog-presence gate** (YOLOv8n via ONNX) checks the photos first; if none shows a dog it responds `no_dog` and the UI offers *save anyway*. On pass (or override) → photos to S3, rows to `sightings`/`photos`. Toast, return to camera. _(Added 2026-07-25; see `docs/OPERATIONS.md`.)_

**Edge cases:**
- GPS denied/slow → capture still succeeds; `geo_source='none'`, flagged for optional manual pin later. Capture is never blocked on a fix.
- GPS present → `geo_source='device_gps'`, `geo_accuracy_m` from the browser.
- No dog detected → warn + **save-anyway** override. High-recall threshold, and the gate **fails open** on any detector error so a real dog is never blocked. Offline captures bypass the gate (respect the user's save intent).
- Offline → queue the capture locally (PWA/service worker + IndexedDB) and sync when signal returns.

### Screen 2 — IndieDex (browse the collection)
- **Map** (MapLibre GL): each sighting a pin at its `geog`, clustered when zoomed out. Tap a pin → photo + time + accuracy.
- **Gallery**: reverse-chronological grid of photo cards. Tap → detail (photo, when, where).
- Both read one endpoint, `GET /dex` → the caller's sightings as JSON. When re-ID arrives, the same view starts grouping pins/cards by `individual_id` — no rebuild.

---

## 5. Auth — pluggable; passcode + magic-link now, social later

_Updated 2026-07-25: a shared-passcode gate was added alongside magic-link for pilot onboarding, and sessions were made persistent. The seam below is unchanged._

Auth is built behind a **provider interface** from day one, so sign-in methods are swappable and additive without touching session handling or the app:

- **`AuthProvider` seam:** the app depends on "resolve a request → an authenticated `observer`," not on any specific sign-in method. Magic-link is the first provider; **Google / Facebook OAuth are future providers that plug into the same seam** with no rework of sessions, the observer model, or app code. (This is Akash's explicit call — build auth modular for later social login.)
- **Magic-link provider:** a server signing key mints a per-user link; opening it establishes an `httpOnly` session cookie → that browser is a known `observer`. No passwords. Adding a tester = minting one more link.
- **Passcode gate (`/join`) — closed-pilot onboarding:** a shared passcode admits testers who enter a name, minting the same session. Lets the pilot widen without minting a link per person; observers tagged `created_via='passcode'`.
- **Sessions are provider-agnostic and persistent:** every write is tied to the session's `observer_id` regardless of how the session was created. The cookie is long-lived (~400 days) so testers stay logged in across PWA restarts. `phone_hash` (HMAC with server-side secret, `build-foundations.md` §10 / D6) stored when a number is attached.
- **Rollout direction:** passcode → email + allowlist (Resend) → open email signup → Google sign-in, each via the same seam. Detailed in `docs/OPERATIONS.md`.
- The whole app sits behind an authenticated session: no session, no access. Matches `docs/specs/2026-07-10-design.md` Slice 2 ("gated to Akash first, then a small allowlist").

---

## 6. Guardrails honored in this slice

| Guardrail (source) | How the MVP honors it |
|---|---|
| `phone_hash` = HMAC + server secret, never raw numbers (D6) | Implemented in the ingest path from day one |
| UUIDv7 PKs, no enumerable IDs in URLs (D5) | All PKs uuidv7; `/dex` and detail URLs use them |
| Two Postgres roles; `public_read` sees only aggregate views (Slice 1) | `public_read` role created in the migration; **no public endpoints exist yet**, so nothing is exposed |
| Webapp gated to an authed allowlist (Slice 2) | Magic-link session cookie; no anonymous access |
| DPDP soft-delete; sighting kept, observer link severed (D10) | `observers.deleted_at` in schema; deletion path itself deferred until needed |
| Enums = `text` + CHECK; `updated_at` on mutable tables (D10) | Applied in the migration |
| Open code (MIT) ≠ open data (restricted) | Project-level; no data is published by this slice |

---

## 7. Acceptance criteria (MVP done when)

1. Migration applies clean on fresh Postgres + PostGIS + pgvector, all 10 tables + indexes present; `public_read` role exists and cannot see `individual_id` or raw `geog`.
2. From a phone, over HTTPS, Akash can photograph a dog and have a sighting with accurate GPS + time land in `sightings`/`photos`, photo bytes in S3.
3. Access is gated by a session; a minted magic-link **or** the `/join` passcode admits a tester, absence of a session denies all access. Auth sits behind an `AuthProvider` seam (magic-link is one provider; adding Google/Facebook later requires no change to sessions or app code).
4. Multiple distinct observers can each log in and capture independently; each observer's IndieDex shows only their own sightings.
5. `phone_hash` is HMAC; all PKs are uuidv7.
6. The IndieDex renders every one of the caller's sightings as map pins (MapLibre) **and** a gallery grid; tapping either opens a detail view.
7. Capture succeeds with GPS denied (flagged `geo_source='none'`) and survives an offline→online round trip (queued then synced).
8. The app is installable as a PWA (home-screen icon, standalone display).

---

## 8. Deferred, but designed-for (the upgrade path)

| Later | What it adds | What it touches in this MVP |
|---|---|---|
| Re-ID (Slices 4/8) | embed worker + match query + threshold decision | Reads `photos`, writes empty tables, promotes `individual_id`. **No schema/app change.** |
| Individuals (Slice 3) | manual grouping + naming + clinical records | New writes to empty `individuals`/`confirmations`; IndieDex view groups by `individual_id`. |
| Public surface (Slice 7) | heatmap + population w/ CI | New `public_read`-backed views + endpoints; role already exists. |
| WhatsApp intake (Slice 6) | public capture client | New client of the unchanged `POST /sighting`. |

---

## 9. Risks & assumptions

- **Secure-context dependency.** Camera + geolocation require HTTPS; TLS (Caddy auto-cert) is therefore a day-one prerequisite, not a nicety. A valid domain/subdomain is needed.
- **Browser GPS quality varies.** Urban multipath can inflate `geo_accuracy_m`; we store the radius rather than pretend precision, so later re-ID can weight accordingly.
- **Single-box durability.** Pilot runs on one Lightsail VM; Postgres backups (snapshot + `pg_dump` to S3) are part of provisioning, not an afterthought — the captured data is the point.
- **Scope discipline.** The temptation will be to "just add grouping." Resist: individuals are Slice 3, gated behind this MVP shipping and being used.
