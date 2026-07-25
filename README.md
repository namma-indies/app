<h1 align="center">Namma Indies · IndieDex</h1>

<p align="center"><em>every street dog, known and named</em></p>

<p align="center">
  <a href="#license"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-a5502e.svg"></a>
  <img alt="Python 3.12" src="https://img.shields.io/badge/python-3.12-blue.svg">
  <img alt="Status: MVP" src="https://img.shields.io/badge/status-MVP-a5502e.svg">
</p>

---

**Namma Indies** *("our indies" — `namma` is "ours" in Kannada; *indies* are India's street/community dogs)* is an open, community-built system for photographing street dogs, estimating dog **populations** over time, and — the hard part — re-identifying **individual** dogs across sightings so a photo history can become a longitudinal health record.

This repository is the **IndieDex** — the capture-and-collect app that feeds that system. It's a mobile **PWA**: you photograph a street dog, it records where and when, and you browse your sightings on a map and in a gallery. Every sighting is stored against an individual "slot" that starts empty and can be filled later — by hand or, eventually, by re-identification.

> **Status: MVP.** Capture and browse work end-to-end. Individual re-identification (the ML core) is deliberately **not** built yet — the schema is designed so it drops in with no migration. See the [roadmap](#roadmap).

## Principles

- **Open code, restricted data.** The code is MIT-licensed and open. The *data* is not: nothing that resolves a vulnerable individual animal's whereabouts is made public. These are two separate decisions, designed in from the start.
- **Aggregate-only public surface.** Any public view is aggregate (density, population estimates with confidence intervals). Individual-level location is internal and access-controlled.
- **The empty slot.** Every sighting starts anonymous and points at an individual that may stay unnamed for months — until a human recognizes it or the model earns confidence. Recognition-as-love and recognition-as-label are the same event in the data model.

## Features (what works today)

- 📷 **Mobile capture** — snap a street dog; automatic GPS + timestamp; installable as a home-screen PWA.
- 🗺️ **IndieDex** — browse your sightings as photo pins on a map and as a gallery, per contributor.
- 🏷️ **Optional structured fields** — sex, ear-notch (sterilization marker), condition, notes — all optional, stored flexibly.
- 🔐 **Passwordless auth** — magic-link sign-in behind a pluggable provider seam (social login drops in later).
- 🖼️ **Privacy-aware photos** — EXIF/GPS metadata stripped on upload; a full-fidelity original is kept for future re-ID plus a thumbnail for the gallery.
- 🧱 **Re-ID-ready schema** — the full data model (individuals, embeddings, match proposals, confirmations) ships from day one, empty, waiting.

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

The MVP collects data; these light up the tables that already exist:

- [ ] **Individuals & manual identity** — group sightings into named dogs by hand.
- [ ] **Re-identification** — an embedding worker + geo/time-priored candidate matching (the core research bet).
- [ ] **Video capture** — record a clip → extract diverse frames as one multi-view sighting *(in review: [#3](https://github.com/namma-indies/app/pull/3))*.
- [ ] **Public heatmap + population estimates** with honest confidence intervals.
- [ ] **WhatsApp intake** for public contribution.

Design details live in [`docs/`](docs/) and [`build-foundations.md`](build-foundations.md).

## Contributing

Early days — issues and PRs welcome. If you're picking something up, open or comment on an issue first so we can point you at the relevant spec in `docs/`.

## License

Code is licensed under the [MIT License](LICENSE).

**Data is not open.** Contributed photos and any individual-level location data are restricted and are never published in a form that could resolve a specific animal's whereabouts.
