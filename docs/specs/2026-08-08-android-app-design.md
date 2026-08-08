# Native Android app (Play Store internal testing) — design

_Author: Claude, 2026-08-08. Product call (Android, internal-testing-track-first,
mirrors the TestFlight pilot) made by Akash; this doc covers the technical shape._

## Goal

Get Namma Indies installable as a real Android app via Play Console's internal
testing track, without touching the backend or the existing iOS/web pilots. The
web app and the TestFlight build keep running exactly as they do today — the
Android app is a third distribution channel sharing the same `frontend/` code.

## Architecture

Same pattern as the iOS build: wrap the existing `frontend/` build with
Capacitor, producing a new `frontend/android/` Gradle project alongside the
existing `frontend/ios/` one. No backend changes: the wrapped app calls the
same `https://app.nammaindies.org/api` endpoints the PWA and iOS app already
use.

```
frontend/
  src/            (unchanged — shared between web PWA, iOS app, and Android app)
  dist/           (vite build — bundled into both native shells)
  ios/            (existing)
  android/        (new — Capacitor's generated Gradle project)
  capacitor.config.ts   (existing — android block added, no new file)
```

**Reuses, unchanged from the iOS build:**
- `@capacitor/camera` and `takePhotoIfNative()` (`frontend/src/capture/takePhoto.ts`)
  — it already branches on `Capacitor.isNativePlatform()`, not per-OS, so no new
  capture code for Android.
- Auth (passcode / magic link / email allowlist) — same flows, same cookies.
- Dog detection, sightings, offline sync (IndexedDB via `idb`) — all client
  logic in `src/` is shared as-is across all three build targets.
- `.github/workflows/deploy.yml` (web) and `deploy-ios.yml` — both untouched.

**What's new:** Android manifest permissions (camera, storage) — the
`NSCameraUsageDescription`-equivalent step — and an icon/splash generation pass
targeting `--android` instead of `--ios`, from the same `public/icons/icon-512.png`
source.

## Update model

Same two-cadence split as iOS:

- **Backend/API changes** ship on merge to `main` as today; the Android app
  picks them up automatically since it calls the same live API.
- **Frontend UI changes** require a new native build + upload to the internal
  testing track. No live-update path, same reasoning as iOS (avoid ambiguity
  around remotely updating native app code) — and per Google policy, the app
  binary can't reach production/public listing until it clears a continuous
  closed-test period anyway, so a manual build step costs nothing extra right
  now.

## Signing — where this diverges from iOS

iOS needed `fastlane match` plus a private `ios-certs` repo because Apple
requires self-managed signing certificates. Android uses **Play App Signing**:
Google holds the actual app-signing key, and CI only needs a locally-generated
**upload keystore** (one `keytool` command, run once, no certificate-storage
repo). Mechanically simpler, but a one-way choice — once the first release
uses Play App Signing there's no reverting to self-managed signing for this
app. This is the current default Google steers every new app toward, so it's
treated as decided, not open.

## Deploy pipeline

New workflow, `.github/workflows/deploy-android.yml`:

- **Trigger: manual (`workflow_dispatch`)**, matching `deploy-ios.yml` — a Play
  Console upload is a deliberate release, not a continuous deploy.
- **Runner: `ubuntu-latest`** (GitHub-hosted; Android builds don't need macOS,
  unlike iOS).
- **Fastlane** drives the build, reusing the same gem already vendored for
  iOS: `npx cap sync android` → `fastlane build` (Gradle assemble/bundle,
  signed with the upload keystore) → `fastlane supply` (Fastlane's Play
  Console uploader, the Android equivalent of `pilot`) targeting the
  **internal testing track**.
- **Auth via a Play Console service-account JSON key** (generated once in
  Play Console → Setup → API access → create service account, then grant it
  Release access in Users and permissions), stored as a repo secret — the
  Android equivalent of `ASC_API_KEY`. No Google account password or 2FA
  prompt in CI.
- On success, testers already added to the internal testing list get the
  build automatically via the Play Store app — same experience as a
  TestFlight notification, just without the push.

New repo secrets required (mirrors the existing `ASC_API_KEY` /
`DEPLOY_SSH_KEY` pattern): `PLAY_SERVICE_ACCOUNT_JSON`,
`ANDROID_KEYSTORE_BASE64`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`,
`ANDROID_KEY_PASSWORD`.

## Testing

No new automated tests — this is a packaging layer, not new app logic
(same reasoning as the iOS build). Manual verification per build: install via
the internal testing track, run through the existing tester checklist (sign in
via passcode/email, capture a sighting, confirm it appears on the map).

## Out of scope (parked)

- **Public Play Store listing** — store graphics, the Data Safety form,
  content rating questionnaire, and the closed-test-to-production promotion.
  Blocked on Google's mandatory closed-test period (12+ testers, 14 continuous
  days) regardless of when we build the listing assets, so there's no rush to
  do this before the pipeline itself works. Revisit alongside the equivalent
  iOS public-App-Store-listing work, per the parked item in
  `docs/specs/2026-08-05-ios-app-design.md`.
- Push notifications, native-only features beyond the camera plugin (same as
  iOS).

## What only Akash can do (Google-account-tied steps)

These need your Google Play Console identity/account and can't be done by
Claude. Sequenced against the implementation plan below:

1. **In progress:** finish Play Console developer account verification
   (identity document, Android device check, phone number) — already started.
2. **Before first CI run:** create the app record in Play Console (package
   name `org.nammaindies.app`, app name "Namma Indies") and add yourself (and
   any pilot testers) to an internal testing list under Testing → Internal
   testing.
3. **Before first CI run:** generate the upload keystore locally
   (`keytool -genkeypair -v -keystore upload-keystore.jks -alias upload -keyalg RSA -keysize 2048 -validity 9125`)
   and set the four `ANDROID_*` secrets from it.
4. **Before first CI run:** create a service account (Play Console → Setup →
   API access → create new service account via Google Cloud Console), grant
   it "Release manager" access under Users and permissions, download its JSON
   key, set it as `PLAY_SERVICE_ACCOUNT_JSON`.
5. **After first successful upload:** confirm the build shows up for internal
   testers in the Play Store app (may take a few minutes, unlike TestFlight's
   near-instant availability).

Everything else — the Capacitor wrapper, manifest permissions, the GitHub
Actions workflow, the Fastlane config — is implementation and doesn't need you
until it's ready to test.
