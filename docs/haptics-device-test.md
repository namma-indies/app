# Haptics: what buzzes where, and how to check on a phone (#142)

Nobody can feel a haptic in CI, so tests use mocks; this is the real-device check.

## What buzzes where

| Where | Channel | Moments |
| --- | --- | --- |
| iPhone / Android **app** | `@capacitor/haptics` | all |
| **Android browser** (Chrome) | `navigator.vibrate`, after the first tap on the page | all |
| **iPhone browser** (Safari, Chrome-on-iOS; iOS 18+) | hidden `<input type=checkbox switch>` toggled through its label | only moments started by a tap |
| iPhone browser before iOS 18, desktop | none | none |

Moments: `tick` (chip pick/clear, confetti taps), `filled` (a suggestion used), `logged` (Save confirmed / upload confetti), `recordStart`, `recordStop`.

The voice-note suggestion animation has its own timed pattern, which builds with what is on screen (see "The suggestion pattern" below).

## The suggestion pattern (#144)

When a transcript with suggestions is shown, the animation is felt as well as seen. All times come from the animation's own timings (`suggestionFlow.ts`) and follow `--fx-pace`. At pace 1:

| When | What you see | What you feel |
| --- | --- | --- |
| ≈0.36–0.72 s | the shimmer crosses the transcript | a **soft** tick each time it crosses a phrase of an *open* suggestion (left to right, at most 6) |
| beat + 0.19 s | the phrase lights up | a **light** tap |
| beat + 0.6…1.3 s | its words fly to the chip | 3 ticks, **light → medium → firm**, getting closer together (175, 125, 85 ms apart) |
| beat + 1.3 s | the comet lands (ring, "woof", stamp) | a **heavy** tap; the **last** one is a **success** |

Beats are 1.3 s apart, starting at 0.5 s.

Rules:
- Only suggestions still open are felt. Used ones replay silently.
- Pulses less than 70 ms apart are felt as one, and the stronger one wins.
- A flight stops being felt if its chip is answered on the way.
- A chip off screen gets its light-up only.
- At most 24 pulses per showing. Flights lose their first tick before the scan is thinned, and light-ups and landings always stay. With 2–4 suggestions nothing is trimmed.
- With **reduced motion** nothing moves, and one **light** tap says the suggestions are there.

| Where | How the levels are made |
| --- | --- |
| iPhone / Android **app** | `@capacitor/haptics` presets. Soft is a selection tick; light, medium and firm are Light, Medium and Medium impacts; heavy is a Heavy impact; success is the Success notification. |
| **Android browser** | Buzz length stands in for strength: 6, 10, 16, 22 and 32 ms; success is 28 · 60 · 40. |
| **iPhone browser** | Only within about a second of a tap: opening a sighting by tapping it can tick the first scan ticks, as identical flat ticks. Everything later, and suggestions that arrive on their own after a voice note, stay silent. |

The Haptics check has a **Play the suggestion pattern** button. It plays the same pulses for a made-up showing (four phrases, three suggestions, about 4.5 s) without the animation.

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

### The suggestion pattern, on every device
1. Open the Haptics check and tap **Play the suggestion pattern**. Expect, over about 4.5 s:
   - a couple of soft ticks (the scan);
   - then, three times: a light tap, three ticks getting firmer and faster, and a heavy tap;
   - the third landing is a success buzz instead.
   The log lists each pulse as `suggestion <level> ...: ok`.
2. In the real app, open a sighting with a voice note whose suggestions are still open. Feel the same build-up, in step with the shimmer, each phrase lighting up, the comet's flight and its landing.
3. Use or dismiss every suggestion and reopen the sighting. The replay should be silent.
4. Turn on reduced motion and reopen a sighting with open suggestions. Nothing moves, and you feel one light tap.
   - iPhone: Settings > Accessibility > Motion > Reduce Motion.
   - Android: Settings > Accessibility > Remove animations.
5. iPhone Safari: the button ticks only at the start (the tap is fresh). That is the expected limit.

## If something doesn't buzz

Read the log in the check screen: it says `failed (...)` for a native or vibrate error, `silent (...)` when no channel applies, and `dropped` when another buzz was still running.

## Notes for releases

- `frontend/ios/App/Podfile` already lists `CapacitorHaptics`; `Podfile.lock` predates it. Pods are git-ignored and CI runs `npx cap sync ios`, which runs `pod install` and adds the missing pod, so the lock is only stale, not a blocker. Commit a regenerated lock from a Mac with a current CocoaPods when convenient.
- Builds only reach testers when distributed: `deploy-ios.yml`'s `distribute` job runs for `v*.*.*` tags, so a manual (`workflow_dispatch`) build is uploaded but not distributed.
