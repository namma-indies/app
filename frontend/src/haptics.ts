import { Capacitor, registerPlugin } from "@capacitor/core";
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
 *
 * Besides the moments there is one timed shape that follows an animation: a score
 * (playScore), pulses at levels from soft to success, used by the suggestion
 * animation (#144) so the feel builds with what is on screen. In the apps, where the
 * phone can, a score is played as smooth curves by the app's own HapticCurves plugin
 * (#145: Core Haptics on iOS, VibrationEffect on Android); everywhere else, and on
 * an app build without it, as a ladder of separate pulses.
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

/**
 * How strong one pulse of a score is, softest first. Each has a native effect from the
 * plugin's presets and, for Android browsers (which have no strength setting), a buzz
 * whose length stands in for strength: a short one feels softer, a longer one stronger.
 */
export type Level = "soft" | "light" | "medium" | "firm" | "heavy" | "success";
export const LEVELS: Level[] = ["soft", "light", "medium", "firm", "heavy", "success"];
const selection = async () => { await Haptics.selectionStart(); await Haptics.selectionChanged(); await Haptics.selectionEnd(); };
const LEVEL: Record<Level, { native: () => Promise<void>; nativeMs: number; web: number[] }> = {
  soft: { native: selection, nativeMs: 30, web: [6] },
  light: { native: impact(ImpactStyle.Light), nativeMs: 50, web: [10] },
  medium: { native: impact(ImpactStyle.Medium), nativeMs: 70, web: [16] },
  firm: { native: impact(ImpactStyle.Medium), nativeMs: 70, web: [22] },
  heavy: { native: impact(ImpactStyle.Heavy), nativeMs: 80, web: [32] },
  success: { native: notify(NotificationType.Success), nativeMs: 130, web: [28, 60, 40] },
};
/** The web vibrate pattern for one pulse at this level (for tests and the Haptics check). */
export const webPulse = (level: Level) => [...LEVEL[level].web];

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
/** Until when a score's pulse is still buzzing: a moment waits for it, a score's own next pulse doesn't. */
let scoreBusyUntil = 0;

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
    const wait = Math.max(0, busyUntil - now, scoreBusyUntil - now);
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
    const begin = () => { if (wait === 0) start(); else setTimeout(start, wait); };
    // A curve still playing is stopped first, so its later stop can't cut this moment (see releaseCurves).
    if (channel === "native" && liveCurves.size) void releaseCurves().then(begin);
    else begin();
  } catch (error) {
    // Never let a buzz break the thing it was decorating.
    report(`${moment}: failed (${error instanceof Error ? error.message : String(error)})`);
  }
}

/**
 * One pulse of a score: `at` ms from the start, and, optionally, a last check just before
 * it plays. `ladderOnly`: it stands in for a ramp, so it is played only when the ramp
 * can't be (no curves on this phone).
 */
export interface Pulse { at: number; level: Level; still?: () => boolean; ladderOnly?: boolean }
/** Strength building from one level towards another over `duration` ms (a comet's flight), with its own check, asked as it starts and while it plays. */
export interface Ramp { at: number; duration: number; from: Level; to: Level; still?: () => boolean }

// ---- curves (#145) -----------------------------------------------------------------

type Feel = { intensity: number; sharpness: number };
interface HapticCurvesPlugin {
  capabilities(): Promise<{ curves: boolean; engine: string }>;
  play(score: { taps: (Feel & { at: number })[]; ramps: { at: number; duration: number; from: Feel; to: Feel }[] }): Promise<{ id: string }>;
  stop(options: { id: string }): Promise<void>;
}
let curvesPlugin: HapticCurvesPlugin | null = null;
const curvesBridge = () => (curvesPlugin ??= registerPlugin<HapticCurvesPlugin>("HapticCurves"));

/** Each level as a Core Haptics-style tap (0..1). Android has no sharpness, and ignores it. */
const FEEL: Record<Exclude<Level, "success">, Feel> = {
  soft: { intensity: 0.35, sharpness: 0.9 },
  light: { intensity: 0.5, sharpness: 0.5 },
  medium: { intensity: 0.65, sharpness: 0.55 },
  firm: { intensity: 0.8, sharpness: 0.6 },
  heavy: { intensity: 1, sharpness: 0.8 },
};
/** A success: a tap, then a stronger one. */
const SUCCESS_TAPS = [{ at: 0, intensity: 0.75, sharpness: 0.5 }, { at: 110, intensity: 1, sharpness: 0.7 }];
/** A continuous buzz feels stronger than a tap at the same intensity, so a ramp runs below its levels. */
const RAMP_STRENGTH = 0.7;
const rampFeel = (level: Level): Feel => {
  const { intensity, sharpness } = level === "success" ? FEEL.heavy : FEEL[level];
  return { intensity: intensity * RAMP_STRENGTH, sharpness };
};

/** undefined: not asked yet. null: no curves here. Otherwise the engine ("corehaptics", "composition", "amplitude"). */
let curvesEngine: string | null | undefined;
let curvesAsked: Promise<string | null> | null = null;
const curvesInstalled = () => {
  try {
    return native() && Capacitor.isPluginAvailable("HapticCurves");
  } catch {
    return false;
  }
};

/** Whether this phone plays smooth curves, and with what: null when it doesn't (browsers, old app builds, plain vibrators). */
export function hapticCurves(): Promise<string | null> {
  curvesAsked ??= (async () => {
    if (!curvesInstalled()) return null;
    try {
      const { curves, engine } = await curvesBridge().capabilities();
      return curves ? engine : null;
    } catch {
      return null;
    }
  })().then((engine) => (curvesEngine = engine));
  return curvesAsked;
}

/**
 * Play a score: each pulse at its time, with timers, so nothing waits on it. Returns
 * the way to stop the pulses that haven't played yet.
 *
 * A score is decoration: a pulse that would land while a moment is buzzing (a person's
 * own Save, a filled field) is dropped rather than delayed, a pulse whose `still()` says
 * no is skipped, and in iPhone browsers a pulse only ticks right after a tap (one flat
 * tick whatever its level). Each pulse is played on its own, so on Android browsers a
 * score never replaces a moment's longer pattern with one of its own.
 */
export function playScore(pulses: Pulse[], label = "score", ramps: Ramp[] = [], { ladder = false } = {}): () => void {
  if (!ladder && curvesInstalled()) {
    if (curvesEngine === undefined) {
      // Not known yet whether this phone has curves: ask (a moment), then play what is left.
      const asked = Date.now();
      let stopped = false;
      let stop = () => {};
      void hapticCurves().then(() => {
        const late = Date.now() - asked;
        if (!stopped) stop = playScore(pulses.map((p) => ({ ...p, at: p.at - late })), label, ramps.map((r) => ({ ...r, at: r.at - late })));
      });
      return () => { stopped = true; stop(); };
    }
    if (curvesEngine) return playCurves(pulses, ramps, label, curvesEngine);
  }
  return playLadder(pulses, label);
}

/** A curve playing (or about to), so a person's moment can stop it first. `dead`: stopped for a moment; never stop it again. */
interface LiveCurve { id: Promise<string | null>; dead: boolean }
const liveCurves = new Set<LiveCurve>();

/**
 * Stop every curve that is playing, before a moment plays: on Android a stop is
 * Vibrator.cancel(), which would also cut a moment that started after it. Each stopped
 * curve is marked dead, so a later stop (its chip answered) never reaches the phone.
 */
function releaseCurves(): Promise<unknown> {
  const stops = [...liveCurves].map((curve) => {
    curve.dead = true;
    return curve.id.then((id) => (id ? curvesBridge().stop({ id }) : undefined)).catch(() => {});
  });
  liveCurves.clear();
  return Promise.all(stops);
}

/** How often a playing ramp asks whether it should go on. */
const RAMP_CHECK_MS = 100;

/**
 * A score as native curves, in pieces: each ramp with the taps during and at the end of
 * it (a flight and its landing), and each other tap on its own. A piece starts at its
 * own time, only if every check in it still says yes (a suggestion dismissed before its
 * comet launches never starts buzzing), and a ramp asks again every RAMP_CHECK_MS while
 * it plays. Starting each piece at its first tap also keeps Android, which replaces
 * whatever is buzzing when a new effect starts, from cutting the piece before. If a
 * moment is buzzing when a piece would start, that piece is played as the ladder, which
 * gives way pulse by pulse.
 */
function playCurves(pulses: Pulse[], ramps: Ramp[], label: string, engine: string): () => void {
  type Piece = { at: number; end: number; pulses: Pulse[]; ramps: Ramp[] };
  const pieces: Piece[] = ramps.map((ramp) => ({ at: ramp.at, end: ramp.at + ramp.duration, pulses: [], ramps: [ramp] }));
  for (const pulse of pulses) {
    const piece = pieces.find((p) => p.ramps.length && pulse.at >= p.at && pulse.at <= p.end);
    if (piece) piece.pulses.push(pulse);
    else if (!pulse.ladderOnly) pieces.push({ at: pulse.at, end: pulse.at, pulses: [pulse], ramps: [] });
  }
  const timers: ReturnType<typeof setTimeout>[] = [];
  const stops = new Set<() => void>();
  const name = `${label} curves (${engine})`;

  const play = (piece: Piece) => {
    const checks = [...piece.pulses, ...piece.ramps].flatMap((item) => (item.still ? [item.still] : []));
    if (checks.some((still) => !still())) return;
    if (busyUntil > Date.now()) {
      stops.add(playLadder(piece.pulses.map((p) => ({ ...p, at: p.at - piece.at })), label));
      return;
    }
    const at = (t: number) => Math.max(0, t - piece.at);
    const taps = piece.pulses.filter((p) => !p.ladderOnly).flatMap((p) => p.level === "success"
      ? SUCCESS_TAPS.map((tap) => ({ ...tap, at: at(p.at) + tap.at }))
      : [{ at: at(p.at), ...FEEL[p.level] }]);
    const curve: LiveCurve = {
      id: curvesBridge().play({
        taps,
        ramps: piece.ramps.map((r) => ({ at: at(r.at), duration: r.duration, from: rampFeel(r.from), to: rampFeel(r.to) })),
      }).then(
        ({ id }) => { report(`${name}: ok`); return id; },
        (error) => { report(`${name}: failed (${error instanceof Error ? error.message : String(error)})`); return null; },
      ),
      dead: false,
    };
    liveCurves.add(curve);
    const own: ReturnType<typeof setTimeout>[] = [];
    const stop = () => {
      own.forEach(clearTimeout);
      stops.delete(stop);
      if (!liveCurves.delete(curve) || curve.dead) return;
      void curve.id.then((id) => (id ? curvesBridge().stop({ id }) : undefined)).catch(() => {});
    };
    stops.add(stop);
    // Ask again while it plays: at each of its pulses, and every RAMP_CHECK_MS of a ramp.
    const askAt = new Set(piece.pulses.filter((p) => p.still).map((p) => p.at - piece.at));
    for (const r of piece.ramps) {
      if (r.still) for (let t = RAMP_CHECK_MS; t < r.duration; t += RAMP_CHECK_MS) askAt.add(at(r.at) + t);
    }
    for (const t of askAt) if (t > 0) own.push(setTimeout(() => { if (checks.some((still) => !still())) stop(); }, t));
    // Done: no longer something a moment needs to stop.
    const last = Math.max(piece.end - piece.at, ...taps.map((tap) => tap.at)) + 50;
    own.push(setTimeout(() => { own.forEach(clearTimeout); stops.delete(stop); liveCurves.delete(curve); }, last));
  };

  for (const piece of pieces) timers.push(setTimeout(() => play(piece), Math.max(0, piece.at)));
  return () => {
    timers.forEach(clearTimeout);
    [...stops].forEach((stop) => stop());
  };
}

function playLadder(pulses: Pulse[], label: string): () => void {
  const timers: ReturnType<typeof setTimeout>[] = [];
  const pulse = ({ level, still }: Pulse) => {
    const name = `${label} ${level}`;
    try {
      if (still && !still()) return;
      const channel = hapticChannel();
      if (!channel || (channel === "switch" && !tapActive())) {
        report(`${name}: silent (${channel === "switch" ? "iPhone browsers only tick for a tap, and none just happened" : "no haptics channel here"})`);
        return;
      }
      const now = Date.now();
      if (busyUntil > now) {
        report(`${name}: dropped (another buzz was still running)`);
        return;
      }
      const { native: play, nativeMs, web } = LEVEL[level];
      scoreBusyUntil = now + (channel === "vibrate" ? web.reduce((sum, ms) => sum + ms, 0) : nativeMs);
      if (channel === "native") quietly(play, `${name} native`);
      else if (channel === "switch") quietly(tickSwitch, `${name} ios-switch`);
      else quietly(() => { if (!navigator.vibrate(web)) throw new Error("navigator.vibrate returned false"); }, `${name} vibrate`);
    } catch (error) {
      report(`${name}: failed (${error instanceof Error ? error.message : String(error)})`);
    }
  };
  for (const each of pulses) {
    if (each.at <= 0) pulse(each);
    else timers.push(setTimeout(() => pulse(each), each.at));
  }
  return () => timers.forEach(clearTimeout);
}
