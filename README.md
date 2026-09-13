<h1 align="center">Namma Indies · IndieDex</h1>

<p align="center"><em>every street dog, known and named</em></p>

<p align="center">
  <a href="#license"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-a5502e.svg"></a>
  <img alt="Python 3.12" src="https://img.shields.io/badge/python-3.12-blue.svg">
  <img alt="Status: MVP" src="https://img.shields.io/badge/status-MVP-a5502e.svg">
</p>

---

**Namma Indies** *("our indies" — `namma` is "ours" in Kannada; *indies* are India's street/community dogs)* is an open, community-built system for photographing street dogs, estimating dog **populations** over time, and — the hard part — re-identifying **individual** dogs across sightings so a photo history can become a longitudinal health record.

This repository is the **IndieDex** — the capture-and-collect app that feeds that system. It's a mobile **PWA** (with native iOS and Android wrappers): you photograph a street dog, it records where and when, and you browse sightings on a map and in a gallery. Every sighting is stored against an individual "slot" that starts empty and gets filled by re-identification plus a human confirming it.

> **Status: pilot.** Capture, browse and **re-identification** all run in production. A photo is detected, cropped, embedded, and matched against nearby sightings; a human confirms or rejects the proposal, and a confirmed match mints an individual. Naming those individuals is the next piece — see the [roadmap](#roadmap).
>
> **On re-ID accuracy, honestly:** it is governed by how many photos of that dog are already stored — measured **37% top-1 with one prior photo, 83% with eight**. And on this population look-alikes *outscore* genuine matches, so no similarity cut-off separates them. The system therefore proposes a ranked shortlist and never asserts a match; a person decides. Numbers and method are in [`AGENTS.md`](AGENTS.md).

## Principles

- **Open code, restricted data.** The code is MIT-licensed and open. The *data* is not: nothing that resolves a vulnerable individual animal's whereabouts is made public. These are two separate decisions, designed in from the start.
- **Aggregate-only public surface.** Any public view is aggregate (density, population estimates with confidence intervals). Individual-level location is internal and access-controlled.
- **The empty slot.** Every sighting starts anonymous and points at an individual that may stay unnamed for months — until a human recognizes it or the model earns confidence. Recognition-as-love and recognition-as-label are the same event in the data model.

## Features (what works today)

- 📷 **Mobile capture** — snap a street dog; automatic GPS + timestamp; installable as a home-screen PWA, and shipped as native iOS and Android apps against the same API.
- 🎥 **Or record a clip** — frames are extracted server-side and kept as one multi-view sighting; the video itself is never stored. Eight views of a dog beat one by a wide margin at matching time.
- 🖼️ **Import from your camera roll** — for a dog you photographed before you had the app. It keeps the photo's *own* date and place, read from EXIF; where a file has been stripped, it asks rather than assuming here-and-now.
- 🐕 **Re-identification** — YOLO26x finds the animal, MiewID embeds the crop, and candidates are retrieved within 1 km via PostGIS → HNSW → an exact re-rank. Proposals go to a human; a confirmed verdict mints an individual.
- 🗂️ **Dogs** — the identified animals, one card each, with their photos, how many people have seen them, and a ranked shortlist of look-alikes for review.
- 🗺️ **Map** — sightings as photo pins, clustered; yours by default, or the whole
  contributor cohort's. Your own pins are exact; everyone else's are shown as a
  ~1 km area, because a map that resolves a specific street dog to a specific
  street is useful to someone who means it harm.
- 🏷️ **Optional structured fields** — sex, ear-notch (sterilization marker), condition, notes — all optional, stored flexibly.
- 🚩 **Report and review** — anyone can flag a sighting from the map; it leaves the
  shared surfaces immediately and waits for a moderator, who can restore it or
  keep it down. Nothing is deleted: a photograph is evidence of something that
  happened, and hiding is reversible where deleting is not.
- 🔐 **Passwordless auth** — magic-link sign-in behind a pluggable provider seam, with a shared-passcode door for closed pilots.
- 🔒 **Privacy-aware photos** — capture metadata is read once, then stripped: stored images carry no EXIF, no embedded GPS. A full-fidelity WebP original is kept for the vision models, plus a thumbnail for the gallery.

## Tech stack

| Layer | Choice |
|---|---|
| Backend | Python 3.12 · FastAPI (async) · asyncpg |
| Database | PostgreSQL 16 + **PostGIS** (spatial) + **pgvector** (embeddings) |
| Migrations | Alembic |
| Object storage | S3-compatible (MinIO in dev, AWS S3 in prod) |
| Frontend | React + TypeScript + Vite · MapLibre GL · installable PWA |
| Auth | Magic-link (pluggable `AuthProvider`) |
| Packaging | Docker (multi-stage) · Caddy (auto-TLS) · deployed on a single VM |
| Tooling | [`uv`](https://github.com/astral-sh/uv) (Python) · npm (frontend) |

## Repository layout

```
backend/          FastAPI app, migrations, tests (uv project)
  app/            config, auth, storage, photos, routes, main
  migrations/     Alembic — 0001 is the full schema
  tests/          pytest (unit + integration against Postgres/MinIO)
frontend/         React + Vite PWA (capture + IndieDex screens)
  ios/ android/  Capacitor wrappers — same web build, shipped to TestFlight / Play
docker/db/        Postgres + PostGIS + pgvector image
deploy/           Caddyfile, entrypoint, provisioning notes
docs/             design specs and build notes
Dockerfile                 multi-stage: build PWA → serve with the API
docker-compose.dev.yml     local Postgres + MinIO
docker-compose.prod.yml    Caddy + app + Postgres
build-foundations.md       stack, guardrails, north star
```

## Quick start (local)

**Prerequisites:** [Docker](https://docs.docker.com/get-docker/), [`uv`](https://github.com/astral-sh/uv), Node 22+.

```bash
# 1. Start Postgres (PostGIS + pgvector) and MinIO
docker compose -f docker-compose.dev.yml up -d db minio

# 2. Backend: install deps + apply the schema
cd backend
uv sync
uv run alembic upgrade head

# 3. Run the API (also serves the built PWA at /)
uv run uvicorn app.main:app --reload
```

```bash
# 4. Frontend dev server (in another terminal)
cd frontend
npm install
npm run dev
```

```bash
# 5. Mint yourself a login link (no signup — magic link)
cd backend
uv run python -m app.auth.magiclink mint "Your Name"
```

Open the printed link (it works over `localhost`, a secure origin, so the camera and geolocation work), snap a photo, and watch it appear in the IndieDex.

### Testing from a phone

`localhost` is a secure origin, so the steps above give you the camera and GPS
on the desktop. A phone on the LAN is not localhost, and three things then bite
at once — each of which fails silently:

- **The session cookie is `Secure`.** Over plain http a browser declines to
  store it, so sign-in bounces you straight back to the gate with no error
  anywhere. The dev server therefore serves HTTPS whenever `frontend/.certs/`
  holds a `dev.crt` / `dev.key` pair. Generate one for your LAN IP with any
  self-signed recipe; the directory is gitignored. Expect a certificate warning
  once, and click through it.
- **Camera and geolocation need a secure context**, so LAN testing cannot work
  over http at all.
- **Photos come from MinIO**, which is http. Loaded from an https page they are
  blocked as mixed content — an empty map with no useful console message. The
  dev server proxies the bucket path so the whole app is one origin.

The port is pinned to **5174** deliberately: the backend signs photo URLs and
magic links against `PUBLIC_BASE_URL` / `S3_PUBLIC_ENDPOINT`, so a dev server
that drifted to 5173 would break every image and every sign-in link with no
visible cause.

Point both at the dev server, and start it with whatever ports you actually
have free:

```bash
# backend -- 8000 is often already taken
S3_PUBLIC_ENDPOINT=https://192.168.1.42:5174 \
PUBLIC_BASE_URL=https://192.168.1.42:5174 \
  uv run uvicorn app.main:app --reload --port 8300

# frontend, in another terminal
VITE_API_TARGET=http://localhost:8300 \
VITE_S3_TARGET=http://localhost:9002 \
  npm run dev
```

Keep `--reload` on the backend. Without it the process serves whatever code it
started with, and a branch switch leaves the API answering with an old response
shape while the hot-reloaded frontend expects the new one — which surfaces as a
blank screen rather than an error.

## Testing

```bash
cd backend
docker compose -f ../docker-compose.dev.yml up -d db minio   # integration tests need these
uv run pytest -q

cd ../frontend
npm run typecheck && npm run build
```

Pure-unit backend tests run without Docker; integration tests exercise the real Postgres + MinIO.

## Deployment

The app ships as a single container image; a VM just runs `docker compose`.

```bash
docker build -t indiedex .
cp .env.example .env      # fill in secrets, domain, and S3 credentials
docker compose -f docker-compose.prod.yml up -d
```

Caddy provisions TLS automatically for `$APP_DOMAIN` (HTTPS is required — the camera and geolocation only work over a secure origin). Postgres runs on-box for the pilot; point `DATABASE_URL` at a managed database (e.g. RDS with `postgis` + `vector`) to move it off-box — no code change. See `deploy/provision.md`.

## Roadmap

- [x] **Re-identification** — embedding + geo/time-priored candidate matching, the core research bet. Live.
- [x] **Video capture** — record a clip → diverse frames as one multi-view sighting.
- [x] **Individuals** — confirmed matches mint an identity, browsable in the Dogs tab.
- [ ] **Naming** — a name is a claim of relationship, not a field, so it needs rules about who may name and what happens when two people disagree ([#4](https://github.com/namma-indies/app/issues/4)). Individuals show a number until then.
- [ ] **A public surface** — individual profiles public, but never a named animal's precise *and* current *and* predictable location ([#5](https://github.com/namma-indies/app/issues/5)).
- [ ] **Population estimates** with honest confidence intervals.
- [ ] **WhatsApp intake** for public contribution.

Design details live in [`docs/`](docs/) and [`build-foundations.md`](build-foundations.md).

## Contributing

Early days — issues and PRs welcome. If you're picking something up, open or comment on an issue first so we can point you at the relevant spec in `docs/`.

## License

Code is licensed under the [MIT License](LICENSE).

**Data is not open.** Contributed photos and any individual-level location data are restricted and are never published in a form that could resolve a specific animal's whereabouts.
