# IndieDex — operations & decision log

The running-reality doc: where it's deployed, how to operate it, and the
decisions made shipping the weekend pilot. Design intent lives in
`docs/specs/`; this is what's actually live and how to touch it.

_Last updated: 2026-07-25._

---

## Live deployment (temporary)

- **URL:** https://app.nammaindies.org
- **Host:** a **temporary Dognosis-account Lightsail box** (Seoul, `ap-northeast-2`),
  used while AWS provisions the real Namma Indies VM. No real NI data lives here
  beyond pilot testing.
- **Stack on box:** Caddy (auto-TLS) → app (FastAPI/uvicorn) → Postgres+PostGIS;
  photos in the **NI-account S3 bucket** (already the final home — S3 does not move).
- **SSH:** `ssh -i ~/.ssh/lightsail-ap-northeast-2.pem ubuntu@16.184.62.131`

> This **supersedes** the GHCR-image approach in
> `docs/plans/2026-07-19-indiedex-mvp.md` §deploy. We deploy from a git checkout
> on the box instead (simpler for a fast-moving pilot; revisit CI images later).

### Deploy a change

```bash
# 1. from your laptop: commit + push to main
git push origin main
# 2. on the box: pull + rebuild
ssh -i ~/.ssh/lightsail-ap-northeast-2.pem ubuntu@16.184.62.131
cd ~/app && git pull origin main && \
  sudo docker compose -f docker-compose.prod.yml up -d --build
```

- `~/app` is a **git clone of `github.com/namma-indies/app` (public), branch `main`**.
- `.env` (all secrets + `JOIN_PASSCODE`) is git-ignored and **lives only on the box** —
  never overwrite it on deploy. Rotating secrets = edit `.env`, `up -d`.
- DB and TLS certs persist in named volumes (`app_pgdata`, `app_caddy_data`).
  `compose down` **without** `-v` keeps them.

### Moving to the permanent NI box (later)

Repoint DNS → new IP, `pg_dump | pg_restore`, copy `.env`, `compose up`. S3
doesn't move. That's the whole migration — the point of the Docker setup.

---

## Auth & testers

Two ways in; both mint the same session (persistent cookie, ~400 days — survives
PWA restarts). Sessions are provider-agnostic (`backend/app/auth/base.py` seam).

- **Passcode gate (`/join`)** — the closed-pilot flow. Share the URL + a passcode
  over WhatsApp; tester enters a name and is in. Observers tagged `created_via='passcode'`.
  - Passcode is `JOIN_PASSCODE` in the box `.env`. Change it:
    ```bash
    cd ~/app && sed -i "/^JOIN_PASSCODE=/d" .env && \
      echo "JOIN_PASSCODE=new-code" >> .env && \
      sudo docker compose -f docker-compose.prod.yml up -d app
    ```
- **Magic link** — `uv run python -m app.auth.magiclink mint "<name>" --base-url https://app.nammaindies.org`
  inside the app container; hand the link over. Reusable for 7 days.

### Rollout ladder (agreed direction, not yet built past stage 0)

0. **Passcode gate** — _shipped_.
1. **Email + allowlist** — enter email → if allowlisted, emailed a magic link.
   Reuses the existing token machinery. Sender: **Resend** (not raw SES — SES's
   sandbox-approval gate isn't worth it); verify `nammaindies.org` via DKIM records
   in Cloudflare DNS. (Cloudflare only *hosts* the DNS; it does not send email.)
2. **Open email signup** — drop the allowlist check.
3. **Google sign-in** — added *alongside* email via the `AuthProvider` seam, not a
   rewrite. More setup than email, but removes deliverability entirely.

---

## Dog-presence gate on capture

`/sighting` runs a server-side detector and rejects photos with no dog unless the
user overrides ("save anyway"). See `backend/app/detect.py`.

- **Model:** Ultralytics YOLOv8n via ONNX Runtime (`backend/app/ml/yolov8n.onnx`).
  Presence only — reads the max COCO `dog`-class confidence, no box decode.
- **The one knob:** `DOG_CONF_THRESHOLD` in `detect.py` (currently `0.25`,
  high-recall). Lower = fewer real dogs flagged. Tune from field feedback, redeploy.
- **Fails open:** any detector error → capture proceeds. The gate never wedges a
  real sighting.
- **Offline captures bypass the gate** (can't run the model offline; respects the
  user's save intent — they sync with override set).
- **Server-side by choice:** keeps a 13MB model off phones and lets us tune without
  fighting the PWA service-worker cache.

> **License flag:** YOLOv8n is **AGPL-3.0** (repo is otherwise MIT). Fine for the
> pilot; before public/commercial launch, either get an Ultralytics commercial
> license or swap for a permissive model — `detect.py` is model-agnostic. See
> `backend/app/ml/NOTICE.md`.

---

## Known operational notes

- **Stale PWA / service worker:** the SW must pass `/auth` and `/join` to the
  network (`vite.config.ts` → `navigateFallbackDenylist`), else it serves the
  cached shell and swallows magic-link login + the `/join` page. If a tester's
  installed app misbehaves after an SW change, have them open in a Safari private
  tab or remove+re-add the home-screen app once; `autoUpdate` self-heals after.
- **S3:** the app's IAM has List+Get+Put, **no DeleteObject** — photos can't be
  pruned from the app yet, and a stray `healthcheck/probe.txt` from setup lingers
  (harmless). Add `s3:DeleteObject` when we want app-side deletion.
- **Latency:** box is in Seoul, users/bucket in India (~100ms). Acceptable for a
  temporary test box; resolved by the move to the NI box.

---

## Related design threads (parked, not built)

- **Public individual surface / naming — GitHub #4, #5.** The three-dial
  visibility rule (coarsen to area-label, delay, strip-pattern) + standing model.
  `individuals` stays empty in the MVP; no schema beyond #4's names table needed
  to start. `build-foundations.md`'s "aggregate-only public surface" principle is
  slated to relax to "individual profiles public, precise-current-predictable
  location gated" — update the copy when the public-map feature actually lands.
