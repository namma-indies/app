# iOS native app: cross-origin auth — status and the open fork

_Status note, not a finished design. Written mid-debugging session on
2026-08-05 to capture where things stand before deciding which way to go._

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

## The actual remaining bug

None of the above gets you signed in. Confirmed via Safari Web Inspector
(Storage → Cookies) with a genuinely untouched, single-use token: the server
correctly returns `Set-Cookie: session=...; SameSite=None; Secure` (verified
directly with `curl -H "Origin: capacitor://localhost"` against production),
but **the cookie never lands in the webview's cookie jar.** Nothing under
`Cookies — app.nammaindies.org` ever appears, before or after the request.

Working theory: WebKit's third-party cookie blocking drops a `Set-Cookie`
received via a cross-origin `fetch()` (as `deepLink.ts` uses to consume the
magic link) regardless of `SameSite=None`/`Secure`/`credentials: "include"` —
that's a client-side policy no response header can override. This is
**confirmed for the deep-link fetch path specifically** — not yet confirmed
to apply to *every* fetch path.

**A separate, definite bug, found but not yet fixed:** `SignIn.tsx`'s `post()`
(used by both the email-request and **passcode** doors) never sets
`credentials: "include"` at all. On a cross-origin request this means the
browser won't even consider storing a `Set-Cookie` in the response,
regardless of ITP. This confounds the picture — the passcode door has never
had a fair test in the native app. Next step, not yet done: add
`credentials: "include"` there, rebuild, and test via the passcode door
(faster than email — no token burn, no inbox round-trip) with Web Inspector
open to see whether the cookie sticks this time.

- If it **does** stick → the missing `credentials` was the whole story for
  that path, and a typed one-time-passcode-by-email (Akash's suggestion,
  see below) works with no further changes.
- If it **still doesn't** stick → WebKit is blocking third-party
  cookie-setting unconditionally, for any fetch, and the fork below is real.

## The fork, if cookies are unconditionally blocked

**Option A — native cookie injection.** Handle the consume step in Swift
(native `URLSession`, not the webview's JS `fetch`), then explicitly push
the resulting cookie into the webview's store via
`WKWebsiteDataStore.default().httpCookieStore.setCookie(...)`. Real Swift
work, but keeps the current architecture (bundled local shell, offline-first,
cookie-based sessions matching the web PWA) unchanged.

**Option B — point the webview at the live site** (`capacitor.config.ts`
`server.url` = `https://app.nammaindies.org`) instead of bundling
`frontend/dist` locally. Makes the webview's origin match the API's origin
exactly, so every cross-origin problem (CORS, cookies, Universal Links) stops
existing. **Cost: the app can no longer load its own shell offline** — it's
used outdoors on Bangalore streets, so this is a real product regression,
not just an implementation detail. **This is Akash's call, not a technical
default** — flagging it rather than deciding it.

**Option C — stop using cookies for the native app; use a bearer token
instead.** `require_observer` (`backend/app/auth/deps.py`) currently reads
only the `session` cookie. Extend it to also accept `Authorization: Bearer
<token>`, and have `/auth/join`, `/auth/email/consume` etc. return the
session value in the JSON body (it's already generated via
`issue_session`/`read_session` — same value, just also handed back instead
of cookie-only). The app stores it (Capacitor Preferences or localStorage)
and sends it as a header on every request. No cookies, no `SameSite`, no
CORS-credentials dance, no ITP, no origin change, no loss of the offline
shell. Smallest total diff of the three; doesn't touch `capacitor.config.ts`
at all.

**Akash's suggestion** (send a 6-digit code by email instead of a link, typed
into the existing passcode-style field) is a good simplification of *how the
code reaches the app* — it sidesteps Universal Links entirely, which is
nice regardless of which fork above gets picked. But it does **not** by
itself solve the cookie problem: the code still has to be *submitted*
somehow, and if that submission is a cross-origin `fetch()` and WebKit really
does block third-party cookie-setting unconditionally, the same wall applies.
It composes cleanly with **Option C** though: type the code in, server
validates it and returns the session token in the response body instead of
(or alongside) a cookie, app stores it, done — no link-tap flow, no cookie
problem, no native Swift work.

## Loose end to revisit

`samesite="none"` (deps.py) is live in production right now for a
cross-origin scenario that may not survive whichever fork gets picked. If
the resolution ends up same-origin (Option B) or bearer-token (Option C),
revert it back to `"lax"` — `None` is unconditionally weaker CSRF posture,
and it's currently doing that for the **web PWA too**, not just the native
app, with no benefit to the web path.

## Not yet decided

Which fork (A/B/C), and whether Akash's OTP-by-email idea should replace the
magic-link door entirely or sit alongside it. Needs the `credentials:
"include"` test run first — that determines whether this decision is even
necessary yet, or whether the passcode door already works once that one bug
is fixed.
