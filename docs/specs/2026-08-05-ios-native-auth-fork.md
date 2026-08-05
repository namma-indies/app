# iOS native app: cross-origin auth — resolved

_Written mid-debugging session on 2026-08-05 as a status note; updated
2026-08-06 with the actual resolution. Kept as a record of the debugging
path — the eventual fix (`CapacitorHttp`) is one line, but getting there
needed ruling out two confounding bugs first._

## Resolution

**Fix: `CapacitorHttp: { enabled: true }` in `frontend/capacitor.config.ts`.**
Routes the webview's `fetch()`/`XMLHttpRequest` through native `URLSession`
instead of WKWebView's own networking stack. WKWebView's cookie policy
(reject any `Set-Cookie` whose domain doesn't match the main document's
domain) is specific to *its* stack — `URLSession` uses the app's ordinary
`HTTPCookieStorage`, which isn't subject to that restriction. Confirmed
working end-to-end on-device: sign-in (passcode door) now sticks, and the
photo-upload `FormData` path (the one real risk `CapacitorHttp` is known to
sometimes affect) still works.

No app code changed — every existing `fetch()` call in `api.ts`,
`deepLink.ts`, `SignIn.tsx` is patched transparently by the plugin.

**Left in place, deliberately, not reverted:** the CORS middleware
(`backend/app/main.py`) and `samesite="none"` cookie
(`backend/app/auth/deps.py`) from the earlier attempt. Native `URLSession`
requests don't appear to need either (CORS is a browser-enforced concept;
`URLSession` isn't a browser), but leaving them doesn't hurt anything either
— they're just for a browser-context request against this origin that no
longer happens. Revisit if that's ever confirmed to actually be dead code.

## What was ruled out first (for the next person who hits this)

Two confounding bugs made this harder to isolate than it should have been —
worth knowing about since they'll look identical to "sign-in is broken" if
they recur elsewhere:

1. **`SignIn.tsx`'s `fetch()` was missing `credentials: "include"`** (fixed,
   commit `cd545e9`) — on a cross-origin request the browser won't even
   consider a response's `Set-Cookie` without that. Confounded the
   diagnosis because it made the passcode door fail for a boring reason
   unrelated to the real bug.
2. **Single-use tokens getting silently burned by the debugging process
   itself** — testing a magic link via curl, or tapping it more than once,
   consumes it exactly like a real sign-in would, so a second look always
   shows "expired or already used" regardless of whether the first attempt
   actually worked. Cost real time before we started using strictly
   single-use, freshly-minted tokens per test.

Once both were controlled for, Web Inspector's Storage → Cookies panel gave
the real answer directly: a correctly-formed, credentialed, CORS-approved
`Set-Cookie` response still left `Cookies — app.nammaindies.org` completely
empty. That's what `CapacitorHttp` fixes.

## What's built and confirmed working (all shipped to `main`, deployed)

- Capacitor iOS wrapper (`frontend/ios/`), signed and running on a real
  device via Xcode with a personal-team dev certificate.
- Native camera capture (`src/capture/takePhoto.ts`) with save-to-gallery,
  replacing the old file-input path that never wrote to the camera roll on
  iOS.
- Fastlane + `match` scaffolding for TestFlight (not yet run end-to-end —
  still needs `fastlane match appstore` handoff).
- Backend CORS middleware allowing `capacitor://localhost`
  (`backend/app/main.py`).
- Session cookie changed to `SameSite=None; Secure` instead of `Lax`
  (`backend/app/auth/deps.py`) — needed because the native shell's webview
  origin (`capacitor://localhost`) is genuinely cross-origin from the API
  (`https://app.nammaindies.org`), unlike the web PWA.
- `apple-app-site-association` served, scoped to `/auth/*`, so a tapped
  magic-link email opens directly in the app (Universal Links) instead of
  Safari — confirmed working: tapping a link does land in the app, and the
  app's `deepLink.ts` correctly receives the full URL including the token
  (traced through Capacitor's `SceneDelegate` → `CAPSceneDelegateProxy` →
  `AppPlugin` → `appUrlOpen`).
- `src/apiBase.ts` makes every API call absolute (`https://app.nammaindies.org`)
  instead of relative when running natively — fixes the native app silently
  calling itself instead of the real backend.

## Why this took a fork analysis before the fix was found

At the time, three options were on the table for a cookie problem that
looked unconditional (native Swift cookie injection; pointing the webview at
the live site, losing the offline shell; or a bearer-token auth scheme
instead of cookies). A web search turned up `CapacitorHttp` as the
community's actual answer to this exact, well-documented Capacitor/WKWebView
problem before any of those got built — worth searching for prior art
earlier next time a problem looks this specific to a well-known framework.

Akash's suggestion (send a 6-digit code by email instead of a link, typed
into the existing passcode field) remains a good idea independent of any of
this — it sidesteps Universal Links entirely — but wasn't needed to fix the
cookie problem itself, since `CapacitorHttp` fixes that regardless of how
the code/link reaches the app.
