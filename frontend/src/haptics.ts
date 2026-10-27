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

type Step = { at: number; native: () => Promise<void>; ms: number };

const impact = (style: ImpactStyle) => () => Haptics.impact({ style });
const notify = (type: NotificationType) => () => Haptics.notification({ type });

/** Each moment as steps in time (ms from now). `ms` is the buzz length on the web. */
const MOMENTS: Record<Moment, Step[]> = {
  tick: [{ at: 0, native: impact(ImpactStyle.Light), ms: 8 }],
  filled: [{ at: 0, native: notify(NotificationType.Success), ms: 24 }],
  logged: [
    { at: 0, native: notify(NotificationType.Success), ms: 30 },
    { at: 160, native: impact(ImpactStyle.Light), ms: 14 },
    { at: 280, native: impact(ImpactStyle.Light), ms: 14 },
  ],
  recordStart: [{ at: 0, native: impact(ImpactStyle.Medium), ms: 16 }],
  recordStop: [{ at: 0, native: impact(ImpactStyle.Light), ms: 10 }],
};

function native(): boolean {
  try {
    return Capacitor.isNativePlatform() && Capacitor.isPluginAvailable("Haptics");
  } catch {
    return false;
  }
}

/** Chrome only lets a page vibrate once the person has touched it, and logs a warning if not. */
function webVibrate(pattern: number[]) {
  try {
    if (typeof navigator === "undefined" || typeof navigator.vibrate !== "function") return;
    if (navigator.userActivation && !navigator.userActivation.hasBeenActive) return;
    navigator.vibrate(pattern);
  } catch {
    // Quiet by design.
  }
}

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

export function haptic(moment: Moment): void {
  try {
    const steps = MOMENTS[moment];
    if (!native()) return webVibrate(webPattern(steps));
    for (const step of steps) {
      const fire = () => { try { void step.native().catch(() => {}); } catch { /* quiet */ } };
      if (step.at === 0) fire();
      else setTimeout(fire, step.at);
    }
  } catch {
    // Never let a buzz break the thing it was decorating.
  }
}
