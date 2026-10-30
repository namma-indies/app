# How the web app picks up a new version

Issue #175. Short version: after a deploy, a running client notices within
about 30 minutes (or on resume / reconnect), reloads itself if that loses
nothing, and otherwise shows **"New version ready — Reload"**.

## Moving parts

| Piece | Where | Job |
|---|---|---|
| Service worker | `vite.config.ts` (VitePWA `autoUpdate`) | precaches the build; new SW `skipWaiting`s and claims pages |
| Update checks | `frontend/src/update/appUpdate.ts` | `registration.update()` on load, every 30 min, on resume, on `online` (60 s floor) |
| Reload when safe | same, driven by `UpdateBanner.tsx` | reload on SW takeover; prompt if busy |
| "Busy" signal | `update/reloadGuard.ts` (`useReloadGuard`) | capture draft, recording, review / report / voice-note sheets, photo-description edit |
| Headers | `backend/app/static.py` | `no-cache` on `index.html`, `sw.js`, `registerSW.js`, `manifest.webmanifest`; `immutable` on `/assets/*` |
| Native | `update/registerServiceWorker.ts` | no SW in Capacitor; any existing one is unregistered and its caches cleared |

New screens that hold unsaved work should call `useReloadGuard(active)`.

## Manual verification

Needs two builds of the app served by the real backend (static serving is what
sets the headers). Use Chrome devtools → Application → Service Workers, and the
Network tab.

1. **Headers.** `curl -sI <origin>/sw.js` (also `/`, `/index.html`,
   `/manifest.webmanifest`): `cache-control: no-cache`. `/assets/<hashed>.js`:
   `public, max-age=31536000, immutable`.
2. **Auto reload when idle.** Load build A, sit on the dex/map (nothing open).
   Deploy/serve build B. Within ~30 min — or immediately by switching tab away
   and back after a minute, or devtools → "Update" — the page reloads onto B
   without a tap. No second reload needed.
3. **Prompt when busy.** Load A, take a photo on SPOT (draft open). Serve B,
   trigger an update. The draft stays and a "New version ready — Reload" bar
   appears. Reload applies B. Repeat with: recording a voice note, an open
   capture review sheet, an open report sheet.
4. **Prompt then background.** In the prompt state, finish/discard the draft,
   then background the app (switch tab). It reloads onto B by itself.
5. **First visit.** Fresh profile: no prompt, no reload loop.
6. **Offline.** Offline, resume the app: nothing breaks, no error shown. Back
   online: the check runs.
7. **Native.** In an iOS/Android build, devtools/Safari inspector → no service
   worker registered; `caches.keys()` is empty.

## A tester stuck on an old version (one time)

Clients from before this change have no page-side update logic, so they need a
manual nudge once:

1. Open the app (installed PWA or browser tab) and **reload twice** (pull to
   refresh / ⌘R). The first load lets the new service worker install, the
   second runs it. From then on updates are automatic.
2. If it is still on the old version: browser → site settings → **Clear
   site data** for the app's domain (or devtools → Application → Service
   Workers → Unregister, then Clear storage). Installed PWA on a phone: remove
   it from the home screen and reinstall, or clear site data in browser
   settings. Sign in again afterwards; queued offline sightings live in the
   same storage, so let them sync first if there are any ("couldn't sync"
   badge).
