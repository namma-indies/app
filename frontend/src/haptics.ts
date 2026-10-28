import { Capacitor } from "@capacitor/core";
import { Haptics, ImpactStyle, NotificationType } from "@capacitor/haptics";

/**
 * The app's haptic vocabulary (#122): a few named moments, never raw calls, so
 * the same moment always feels the same.
 *
 *   tick         a light tap: something small landed (a word in its chip)
 *   filled       a suggestion filled its field: a short success
 *   logged       the server confirmed a sighting is saved: success, then a run of taps
 *   recordStart  the microphone is live
 *   recordStop   the note is captured
 *
 * Native uses the Capacitor plugin. The web falls back to navigator.vibrate
 * (Android browsers); iOS Safari has no API, so there it does nothing. It never
 * throws and never logs: an old native build without the plugin, a desktop, a
 * test, a phone with haptics switched off all just stay quiet.
 *
 * Haptics are tied to a moment, not to animation frames, and are not motion:
 * prefers-reduced-motion does not silence them. A moment that is a real event
 * or an answer to the person's own tap (logged, filled, record) always fires; a
 * moment that only accompanies an animation (tick) is the caller's to skip when
 * the animation is skipped.
 */
export type Moment = "tick" | "filled" | "logged" | "recordStart" | "recordStop";

/** `ms`: buzz length on the web. `nativeMs`: roughly how long the native effect lasts (Android's, the longer). */
type Step = { at: number; native: () => Promise<void>; ms: number; nativeMs: number };

const impact = (style: ImpactStyle) => () => Haptics.impact({ style });
const notify = (type: NotificationType) => () => Haptics.notification({ type });
const LIGHT = { native: impact(ImpactStyle.Light), nativeMs: 50 };
const MEDIUM = { native: impact(ImpactStyle.Medium), nativeMs: 70 };
const SUCCESS = { native: notify(NotificationType.Success), nativeMs: 130 };

/** Each moment as steps in time (ms from its start). */
const MOMENTS: Record<Moment, Step[]> = {
  tick: [{ at: 0, ms: 8, ...LIGHT }],
  filled: [{ at: 0, ms: 24, ...SUCCESS }],
  logged: [
    { at: 0, ms: 30, ...SUCCESS },
    { at: 160, ms: 14, ...LIGHT },
    { at: 280, ms: 14, ...LIGHT },
  ],
  recordStart: [{ at: 0, ms: 16, ...MEDIUM }],
  recordStop: [{ at: 0, ms: 10, ...LIGHT }],
};

function native(): boolean {
  try {
    return Capacitor.isNativePlatform() && Capacitor.isPluginAvailable("Haptics");
  } catch {
    return false;
  }
}

/**
 * Moments play one after another. Both a web vibrate call and Android's vibrator
 * replace whatever is still buzzing, so a second moment starting mid-way would
 * cut the first short (a burst of taps would clip the success).
 */
let busyUntil = 0;
/** A moment that would have to wait longer than this is dropped: a late tap means nothing. */
const MAX_WAIT_MS = 700;
const GAP_MS = 60;

/** Alternating buzz / gap lengths, as navigator.vibrate wants them. */
function webPattern(steps: Step[]): number[] {
  const pattern: number[] = [];
  let clock = 0;
  for (const step of steps) {
    const gap = step.at - clock;
    if (pattern.length) pattern.push(gap);
    else if (gap > 0) pattern.push(0);
    pattern.push(step.ms);
    clock = step.at + step.ms;
  }
  return pattern;
}

/** Chrome only lets a page vibrate once the person has touched it, and logs a warning if not. */
function webAllowed(): boolean {
  return typeof navigator !== "undefined" && typeof navigator.vibrate === "function"
    && !(navigator.userActivation && !navigator.userActivation.hasBeenActive);
}

function quietly(run: () => unknown) {
  try { void Promise.resolve(run()).catch(() => {}); } catch { /* quiet by design */ }
}

export function haptic(moment: Moment): void {
  try {
    const steps = MOMENTS[moment];
    const onNative = native();
    if (!onNative && !webAllowed()) return;
    const now = Date.now();
    const wait = Math.max(0, busyUntil - now);
    if (wait > MAX_WAIT_MS) return;
    const span = Math.max(...steps.map((step) => step.at + (onNative ? step.nativeMs : step.ms)));
    busyUntil = now + wait + span + GAP_MS;
    const start = () => {
      if (onNative) {
        for (const step of steps) {
          if (step.at === 0) quietly(step.native);
          else setTimeout(() => quietly(step.native), step.at);
        }
      } else {
        quietly(() => navigator.vibrate(webPattern(steps)));
      }
    };
    if (wait === 0) start();
    else setTimeout(start, wait);
  } catch {
    // Never let a buzz break the thing it was decorating.
  }
}
