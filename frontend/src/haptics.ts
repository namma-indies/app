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
 * Three channels, first that applies wins:
 *   native   the Capacitor plugin, in the iOS / Android apps.
 *   vibrate  navigator.vibrate, in Android browsers, once the person has touched the page.
 *   switch   iPhone browsers (iOS 18+) have no vibrate API, but toggling a hidden
 *            <input type=checkbox switch> through its label gives a system tick. WebKit
 *            only does it for a tap, so it is used only while the page has transient user
 *            activation: a moment started by a tap (chip, Save, using a suggestion,
 *            record start/stop) ticks; one that isn't (confetti after an upload, a comet
 *            landing) stays silent in iPhone browsers. It is a single flat tick, so a
 *            moment's shape (success then taps) becomes a row of identical ticks.
 *
 * It never throws and never logs: an old native build without the plugin, a desktop,
 * a test, a phone with haptics switched off all just stay quiet. What happened is
 * remembered for the hidden Haptics check (see HapticsCheck) via onHapticOutcome.
 *
 * Haptics are tied to a moment, not to animation frames, and are not motion:
 * prefers-reduced-motion does not silence them. A moment that is a real event
 * or an answer to the person's own tap (logged, filled, record) always fires; a
 * moment that only accompanies an animation (tick) is the caller's to skip when
 * the animation is skipped.
 */
export type Moment = "tick" | "filled" | "logged" | "recordStart" | "recordStop";
export const ALL_MOMENTS: Moment[] = ["tick", "filled", "logged", "recordStart", "recordStop"];

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
/**
 * A tick that would have to wait longer than this is dropped: a late tap means nothing. The
 * moments that answer a person's own action (saved, filled, record start/stop) are never dropped.
 */
const MAX_WAIT_MS = 700;
const DROPPABLE: Moment[] = ["tick"];
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

/** Which way a buzz would go right now, or null when it can't. */
export type Channel = "native" | "vibrate" | "switch";

/** Chrome only lets a page vibrate once the person has touched it, and logs a warning if not. */
function webAllowed(): boolean {
  return typeof navigator !== "undefined" && typeof navigator.vibrate === "function"
    && !(navigator.userActivation && !navigator.userActivation.hasBeenActive);
}

/** iPhone / iPad browsers, including an iPad that asks for the desktop site. */
function isIosWeb(): boolean {
  if (typeof navigator === "undefined") return false;
  const ua = navigator.userAgent ?? "";
  return /iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);
}

/** The switch trick: iOS 18+, a browser whose <input> knows `switch`, and not the native app. */
export function switchSupported(): boolean {
  try {
    if (native() || !isIosWeb() || typeof document === "undefined") return false;
    const major = /OS (\d+)_/.exec(navigator.userAgent ?? "")?.[1];
    if (major && Number(major) < 18) return false;
    return "switch" in document.createElement("input"); // the HTML attribute's reflected property, not a CSS value
  } catch {
    return false;
  }
}

/** WebKit gives the tick only to a tap: the page must have been tapped a moment ago. */
function tapActive(): boolean {
  return !!navigator.userActivation?.isActive;
}

let switchLabel: HTMLLabelElement | null = null;
function tickSwitch(): void {
  if (!switchLabel?.isConnected) {
    const input = document.createElement("input");
    input.type = "checkbox";
    input.setAttribute("switch", "");
    input.tabIndex = -1;
    const label = document.createElement("label");
    label.setAttribute("aria-hidden", "true");
    label.style.cssText = "position:fixed;left:-100px;top:0;width:1px;height:1px;opacity:0;pointer-events:none;overflow:hidden";
    label.append(input);
    document.body.append(label);
    switchLabel = label;
  }
  switchLabel.click();
}

export function hapticChannel(): Channel | null {
  if (native()) return "native";
  if (webAllowed()) return "vibrate";
  if (switchSupported()) return "switch";
  return null;
}

type Listener = (outcome: string) => void;
const listeners = new Set<Listener>();
/** Hear what each buzz did (or why it didn't). Returns the way to stop listening. */
export function onHapticOutcome(listener: Listener): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
function report(outcome: string) {
  for (const listener of [...listeners]) { try { listener(outcome); } catch { /* quiet */ } }
}

function quietly(run: () => unknown, label: string) {
  try {
    void Promise.resolve(run()).then(
      () => report(`${label}: ok`),
      (error) => report(`${label}: failed (${error instanceof Error ? error.message : String(error)})`),
    );
  } catch (error) {
    report(`${label}: failed (${error instanceof Error ? error.message : String(error)})`);
  }
}

export function haptic(moment: Moment): void {
  try {
    const steps = MOMENTS[moment];
    const channel = hapticChannel();
    // The switch is a tap-only channel: without a tap in the last moments it cannot tick.
    const usable = channel === "switch" ? tapActive() : channel !== null;
    if (!channel || !usable) {
      report(`${moment}: silent (${channel === "switch" ? "iPhone browsers only tick for a tap, and none just happened" : "no haptics channel here"})`);
      return;
    }
    const now = Date.now();
    const wait = Math.max(0, busyUntil - now);
    if (wait > MAX_WAIT_MS && DROPPABLE.includes(moment)) {
      report(`${moment}: dropped (another buzz was still running)`);
      return;
    }
    // Native and the switch play step by step; the web vibrate call takes the whole pattern at once.
    const stepwise = channel !== "vibrate";
    const span = Math.max(...steps.map((step) => step.at + (stepwise ? step.nativeMs : step.ms)));
    busyUntil = now + wait + span + GAP_MS;
    const play = (step: Step) => (channel === "native" ? quietly(step.native, `${moment} native`) : quietly(tickSwitch, `${moment} ios-switch`));
    const start = () => {
      if (stepwise) {
        for (const step of steps) {
          if (step.at === 0) play(step);
          else setTimeout(() => play(step), step.at);
        }
      } else {
        const pattern = webPattern(steps);
        quietly(() => { if (!navigator.vibrate(pattern)) throw new Error("navigator.vibrate returned false"); }, `${moment} vibrate`);
      }
    };
    if (wait === 0) start();
    else setTimeout(start, wait);
  } catch (error) {
    // Never let a buzz break the thing it was decorating.
    report(`${moment}: failed (${error instanceof Error ? error.message : String(error)})`);
  }
}
