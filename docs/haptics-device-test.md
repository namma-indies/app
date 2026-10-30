# Haptics: what buzzes where, and how to check on a phone (#142)

Nobody can feel a haptic in CI, so tests use mocks; this is the real-device check.

## What buzzes where

| Where | Channel | Moments |
| --- | --- | --- |
| iPhone / Android **app** | `@capacitor/haptics` | all |
| **Android browser** (Chrome) | `navigator.vibrate`, after the first tap on the page | all |
| **iPhone browser** (Safari, Chrome-on-iOS; iOS 18+) | hidden `<input type=checkbox switch>` toggled through its label | only moments started by a tap |
| iPhone browser before iOS 18, desktop | none | none |

Moments: `tick` (chip pick/clear, a comet landing, confetti taps), `filled` (a suggestion used), `logged` (Save confirmed / upload confetti), `recordStart`, `recordStop`.

### Limits of the iPhone-browser trick

- WebKit only gives the tick while the page has transient user activation (a tap just happened). Moments not started by a tap, **confetti after an upload and comet landings, stay silent in iPhone browsers**.
- **Save** buzzes after the server answers. If the phone no longer counts the tap by then, Save is silent in the browser. The "later" buttons in the check screen show exactly this.
- It is one flat tick: `logged` becomes three identical ticks, not a success then taps.
- It is never used inside the native app.

## Open the check

- **Browser:** add `?haptics=check` to the app's address (e.g. `https://<the app>/?haptics=check`). Close removes the parameter.
- **App (iOS / Android):** hold the small "indiedex" brand at the top-left for 2 seconds.

It shows: platform; native plugin available; `navigator.vibrate`; whether the page has been tapped; whether a tap is active right now; whether the iOS switch trick is available; the channel a buzz would use; the app version/build. Then one button per moment, the same moments 1.5 s after the tap, and a log of what each buzz did or why it stayed silent. It changes no data.

## Test steps

Before anything: ringer and vibration on, Low Power Mode off, and (iPhone) Settings > Sounds & Haptics > **System Haptics** on. Android: Settings > Sound > vibration / touch feedback on, battery saver off.

### iPhone app (TestFlight)
1. Note the version/build in the check screen (hold the brand 2 s). It must be a build from after 29 Oct (haptics added in #123).
2. "Native haptics plugin" must say **yes** and channel **native**. If it says no, the build is old or the plugin isn't linked: report this.
3. Tap each moment button. Expect: `tick` light tap; `filled` success buzz; `logged` success then two taps; `recordStart` firmer tap; `recordStop` light tap. The log lines should read `... native: ok`.
4. In the real app: pick a chip, use a suggestion, Save an animal, record a voice note (start and stop).

### iPhone Safari
1. Open `?haptics=check`. Platform should be `web (browser)`, vibrate **no**, iOS switch **yes**, channel **switch** (needs iOS 18 or newer; if the switch says no, nothing can buzz there).
2. Tap each top-row moment: you should feel a tick per tap (`logged` three). Then tap a "later" button: it buzzes (or not) 1.5 s later; note which moments tick.
3. In the real app: pick/clear a chip, use a suggestion, Save, record start/stop should tick. Confetti after an upload and comet landings should be silent (expected).

### Android app
1. Check screen via the 2 s hold: version/build, plugin **yes**, channel **native**.
2. Tap each moment, then repeat the real-app actions above. All moments should buzz.

### Android Chrome
1. Open `?haptics=check`. vibrate **yes**. Before the first tap "page has been tapped" is no; after it is yes.
2. Tap each moment: all buzz, patterns as above. Confetti and comet landings also buzz here (after the first tap).

## If something doesn't buzz

Read the log in the check screen: it says `failed (...)` for a native or vibrate error, `silent (...)` when no channel applies, and `dropped` when another buzz was still running.

## Notes for releases

- `frontend/ios/App/Podfile` already lists `CapacitorHaptics`; `Podfile.lock` predates it. Pods are git-ignored and CI runs `npx cap sync ios`, which runs `pod install` and adds the missing pod, so the lock is only stale, not a blocker. Commit a regenerated lock from a Mac with a current CocoaPods when convenient.
- Builds only reach testers when distributed: `deploy-ios.yml`'s `distribute` job runs for `v*.*.*` tags, so a manual (`workflow_dispatch`) build is uploaded but not distributed.
