# Native iOS app (TestFlight) — design

_Author: Claude, 2026-08-05. Product call (native app, iOS-only, TestFlight-first)
made by Akash; this doc covers the technical shape._

## Goal

Get Namma Indies installable as a real iOS app via TestFlight, without touching
the backend or the web pilot. The web app at `app.nammaindies.org` keeps running
exactly as it does today — the native app is an additional distribution channel,
not a replacement.

## Architecture

Wrap the existing `frontend/` build with **Capacitor**, producing a new
`frontend/ios/` Xcode project alongside the existing web build. No backend
changes: the wrapped app calls the same `https://app.nammaindies.org/api`
endpoints the PWA already uses.

```
frontend/
  src/            (unchanged — shared between web PWA and native app)
  dist/           (vite build — bundled into the native shell)
  ios/            (new — Capacitor's generated Xcode project)
  capacitor.config.ts   (new)
```

**Why Capacitor over a plain WKWebView shell:** a thin webview pointing at the
live URL is simpler to build but risks App Review rejection under Apple's
guideline 4.2 ("a website in an app wrapper isn't an app"). Capacitor bundles
the assets locally and gives access to native plugins, which both strengthens
the review case and improves the actual experience:

- **Camera plugin** replaces the current `<input type="file" capture>` flow.
  This also fixes a known gap called out in `docs/OPERATIONS.md` — on iOS the
  file-input capture never writes to the camera roll, so the server copy is
  the only copy. The native Camera plugin can save to the roll.
- Native splash screen, status bar theming, and app icon — standard Capacitor
  config, no code beyond what's already in `public/`.

**What this does NOT change:**
- Auth (passcode / magic link / email allowlist) — same flows, same cookies,
  Capacitor's webview supports persistent cookies like Safari does.
- Dog detection, sightings, offline sync (IndexedDB via `idb`) — all client
  logic in `src/` is shared as-is between PWA and native builds.
- The `.github/workflows/deploy.yml` web pipeline — untouched, keeps deploying
  backend + frontend web build on every merge to `main`.

## Update model

Two independent release cadences, and it matters which one a change falls into:

- **Backend/API changes** ship the same way they do today — merge to `main`,
  auto-deploys, live in seconds. The native app picks this up automatically
  since it's calling the same live API.
- **Frontend UI changes** require a new native build + TestFlight upload.
  There's no live-update path here on purpose — Apple's rules on remotely
  updating app code are ambiguous enough that building around them isn't
  worth it for a pilot. Expect UI changes to ship to testers on the cadence
  you trigger builds, not continuously.

## Deploy pipeline

New workflow, `.github/workflows/deploy-ios.yml`:

- **Trigger: manual (`workflow_dispatch`)**, not on every push to `main`. A
  TestFlight build is a deliberate release, unlike the continuous web deploy.
- **Runner: `macos-latest`** (GitHub-hosted, Xcode preinstalled — no self-hosted
  Mac needed).
- **Fastlane** drives the build: `npx cap sync ios` → `fastlane build` (archive
  + sign) → `fastlane pilot upload` (ship to TestFlight).
- **Signing via `fastlane match`**: certs and provisioning profiles generated
  once, stored encrypted in a private git repo (or S3), synced into CI on each
  run. Avoids "which machine has the certificate" problems entirely — any
  runner can build after `match` decrypts the profile.
- **Auth via App Store Connect API key** (a `.p8` key + key ID + issuer ID),
  stored as repo secrets. No Apple ID password or 2FA prompt in CI, matching
  how `DEPLOY_SSH_KEY` already keeps credentials out of the repo.
- On success, testers already added in App Store Connect get a TestFlight
  notification automatically — no manual "send to testers" step.

New repo secrets required (mirrors the existing `DEPLOY_SSH_KEY` pattern):
`ASC_API_KEY`, `ASC_KEY_ID`, `ASC_ISSUER_ID`, `MATCH_GIT_URL`,
`MATCH_PASSWORD`.

## Testing

No new automated tests — this is a packaging layer, not new app logic. Manual
verification per build: install via TestFlight, run through the existing
tester checklist (sign in via passcode/email, capture a sighting, confirm it
appears on the map).

## Out of scope (parked)

- Android/Google Play — same Capacitor project could target it later with a
  `frontend/android/` folder and a parallel Fastlane lane, but not built now.
- Public App Store listing (screenshots, privacy nutrition label, full App
  Review) — TestFlight only for now, per the rollout ladder in
  `docs/OPERATIONS.md`.
- Push notifications, native-only features beyond the camera plugin.

## What only Akash can do (Apple-account-tied steps)

These need your Apple ID / Apple Developer membership and can't be done by
Claude. Sequenced against the implementation plan below:

1. **Now:** confirm Apple Developer Program membership is active at
   developer.apple.com/account.
2. **Before first CI run:** create the app record in App Store Connect
   (bundle ID, e.g. `org.nammaindies.app`; app name "Namma Indies").
3. **Before first CI run:** generate an App Store Connect API key (Users and
   Access → Integrations → App Store Connect API → Keys), download the `.p8`
   once (Apple only lets you download it once), note the Key ID and Issuer ID.
4. **Before first CI run:** run `fastlane match init` once locally (needs
   Xcode installed on your Mac) to generate the signing certs and push them to
   the private `match` storage repo.
5. **After first successful build:** add TestFlight testers in App Store
   Connect (your email + anyone else piloting) so they get the install
   notification.

Everything else — the Capacitor wrapper, `capacitor.config.ts`, the GitHub
Actions workflow, the Fastlane config — is implementation and doesn't need you
until it's ready to test.
