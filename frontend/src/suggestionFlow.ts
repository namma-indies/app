import "./suggestionFlow.css";
import { playScore, type Level, type Pulse, type Ramp } from "./haptics";
import { useEffect, useLayoutEffect, useRef, type RefObject } from "react";
import { comet, pixelSvg, print, SOUNDS, type Species } from "./fxSprites";

/**
 * The "magic" around voice-note suggestions (#118, #136): when a transcript is
 * shown, one colourful shimmer sweeps across it; then, one suggestion at a
 * time, the words it was read from light up, gather into a small pixel comet
 * (a bone for a dog, a fish for a cat, a sparkle when we don't know) and arc to
 * the chip they produced, leaving a trail of paw prints. The chip blooms with a
 * rotating ring, the animal says something ("woof", "mrrp") and a paw stamp
 * stays on the chip. Using a suggestion sends a comet into its field.
 *
 * The shimmer, the chip's pop and the stamp are CSS keyframes keyed off data
 * attributes (suggestionFlow.css). Everything that needs a measured position
 * runs with the Web Animations API on a throwaway fixed layer. Only transform
 * and opacity are animated, so it all stays on the compositor. Nothing changes
 * layout, takes focus or catches a tap, and every showing ends in the normal
 * resting state. Decoration only: the marks, chips and field values carry the
 * meaning.
 *
 * The species comes from the nearest [data-fx-species] around the chip or
 * field the comet flies to. The pace is one CSS variable, --fx-pace.
 *
 * It is felt too, building with what is on screen (#144): a soft tick as the
 * shimmer crosses each open phrase, a light tap as a phrase lights up, ticks
 * that get firmer and closer together while its comet flies, and a heavy tap as
 * it lands (a success for the last one). Every time comes from the timings
 * below and the same pace. Where the phone can (#145), the flight is one smooth
 * build instead of separate ticks.
 */

// Base timings at --fx-pace: 1, in ms. One suggestion is a beat of STEP_MS, and
// beats never overlap: the next phrase lights up as the last comet lands.
export const FIRST_AT_MS = 500;   // the first phrase lights up while the shimmer is still going
export const STEP_MS = 1300;      // from one suggestion to the next
export const USED_STEP_MS = 900;  // a used one is quieter, and quicker
const GATHER_AT_MS = 450;         // the lit phrase starts gathering into the comet
const LAUNCH_AT_MS = 600;
const FLIGHT_MS = 700;
export const ARRIVE_MS = LAUNCH_AT_MS + FLIGHT_MS; // the comet lands: a tap, the ring, the stamp
const BLOOM_MS = 1000;
const WORD_MS = GATHER_AT_MS + 300;  // a word's whole glow, from lighting up to gathered
const WORD_PEAK = 0.25;              // where in it the glow is brightest
const LIGHT_AT_MS = WORD_MS * WORD_PEAK; // the phrase is lit (its first word's peak)

// The opening shimmer (fx-shimmer in suggestionFlow.css): a band SHIMMER_WIDTH of the
// transcript wide slides from SHIMMER_FROM to SHIMMER_TO of its own width, ease-in-out,
// brightest SHIMMER_GLOW of the way across. A test keeps these equal to the CSS.
export const SHIMMER_MS = 1100;
export const SHIMMER_WIDTH = 0.7;
export const SHIMMER_FROM = -1.1;
export const SHIMMER_TO = 1.6;
const SHIMMER_GLOW = 0.49;

// What is felt (#144), on the same clock.
const FLIGHT_TICKS = [0.45, 0.7, 0.88];  // when, in the flight, a tick is felt: closer and closer up to the landing
const FLIGHT_LEVELS: Level[] = ["light", "medium", "firm"];
/** The same flight as one smooth build, where the phone can: from the phrase's light tap up to the landing. */
const FLIGHT_RAMP: Ramp = { at: LAUNCH_AT_MS, duration: FLIGHT_MS, from: "light", to: "heavy" };
const MERGE_MS = 70;                     // pulses closer than this are felt as one, so only the stronger plays
export const MAX_PULSES = 24;            // per showing, however many suggestions there are
const MAX_SCAN_TICKS = 6;
/** "each": a soft tick for every open phrase the shimmer crosses. "once": one soft tick for the whole scan. */
const SCAN_TICKS = "each" as "each" | "once";
const MAX_WORDS = 8;              // a long quote still lights up, but briefly
const PRINTS = [0.22, 0.38, 0.54, 0.7, 0.86]; // where along the arc a print is left

export const reducedMotion = () =>
  typeof window !== "undefined" && typeof window.matchMedia === "function" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const canAnimate = () => !reducedMotion() && typeof Element !== "undefined" && typeof Element.prototype.animate === "function";

/** The speed setting: --fx-pace on :root (2 is half speed). */
function pace(): number {
  const value = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--fx-pace"));
  return value > 0 ? value : 1;
}

/** Re-arm a CSS animation that is keyed off a data attribute, with a delay. `soft` is the quieter variant. */
function arm(el: HTMLElement, attr: string, delayMs: number, value = "") {
  el.style.setProperty("--fx-delay", `${Math.round(delayMs)}ms`);
  el.removeAttribute(attr);
  void el.offsetWidth; // restart if it was already playing
  el.setAttribute(attr, value);
}

function wordRects(el: Element): DOMRect[] {
  const out: DOMRect[] = [];
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    for (const m of (node.textContent ?? "").matchAll(/\S+/g)) {
      const range = document.createRange();
      range.setStart(node, m.index!);
      range.setEnd(node, m.index! + m[0].length);
      const rect = range.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) out.push(rect);
    }
  }
  return out;
}

const inView = (r: DOMRect) => r.bottom > 0 && r.top < window.innerHeight && r.right > 0 && r.left < window.innerWidth;
const centre = (r: DOMRect) => ({ x: r.left + r.width / 2, y: r.top + r.height / 2 });
type Point = { x: number; y: number };

/** Where a source's words are right now, so they can be flown after the layout has moved on. */
interface Words { items: { text: string; rect: DOMRect }[]; font: string }
function snapshot(from: Element): Words {
  const texts = (from.textContent ?? "").match(/\S+/g) ?? [];
  return { items: wordRects(from).slice(0, MAX_WORDS).map((rect, i) => ({ text: texts[i] ?? "", rect })), font: getComputedStyle(from).font };
}

const speciesOf = (el: Element | null): Species | null => {
  const value = el?.closest("[data-fx-species]")?.getAttribute("data-fx-species");
  return value === "dog" || value === "cat" ? value : null;
};

/** Throwaway fixed layer for everything in flight: never catches a tap, hidden from assistive tech. */
function makeLayer(): HTMLElement {
  const layer = document.createElement("div");
  layer.className = "fx-layer";
  layer.setAttribute("aria-hidden", "true");
  document.body.appendChild(layer);
  return layer;
}

function place(layer: HTMLElement, className: string, at: Point, html = ""): HTMLElement {
  const el = document.createElement("div");
  el.className = className;
  el.style.left = `${at.x}px`;
  el.style.top = `${at.y}px`;
  if (html) el.innerHTML = html; // static markup from fxSprites, never user text
  layer.appendChild(el);
  return el;
}

interface Run { layer: HTMLElement; k: number; anims: Animation[] }
const play = (run: Run, el: Element, frames: Keyframe[], ms: number, delay = 0, easing = "ease-out") =>
  run.anims.push(el.animate(frames, { duration: ms * run.k, delay: delay * run.k, easing, fill: "both" }));

/**
 * The phrase lights up: each word gets a glowing multicolour copy over it.
 * With `to`, the copies then gather into that point (where the comet is born);
 * without, they just fade where they are.
 */
function lightUp(run: Run, { items, font }: Words, to: Point | null, soft: boolean) {
  items.forEach(({ text, rect }, i) => {
    const word = place(run.layer, `fx-word${soft ? " is-soft" : ""}`, { x: rect.left, y: rect.top });
    word.textContent = text;
    word.style.cssText += `width:${rect.width}px;height:${rect.height}px;font:${font};line-height:${rect.height}px`;
    const gather = to ? `translate(${to.x - (rect.left + rect.width / 2)}px,${to.y - (rect.top + rect.height / 2)}px) scale(0.3)` : "scale(1.04)";
    play(run, word, [
      { transform: "scale(0.94)", opacity: 0 },
      { transform: "scale(1.06)", opacity: 1, offset: WORD_PEAK },
      { transform: "scale(1)", opacity: 1, offset: 0.6 },
      { transform: gather, opacity: 0 },
    ], WORD_MS, i * 40, "cubic-bezier(0.5, 0, 0.3, 1)");
  });
}

/** Quadratic arc from `a` to `b`, bowed sideways towards the middle of the screen so it stays in view. */
function arc(a: Point, b: Point) {
  const dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy) || 1;
  const bend = Math.min(140, Math.max(40, len * 0.35));
  let nx = -dy / len, ny = dx / len;
  const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
  if ((window.innerWidth / 2 - mid.x) * nx + (window.innerHeight / 2 - mid.y) * ny < 0) { nx = -nx; ny = -ny; }
  if (Math.abs(dx) < 4 && Math.abs(dy) < 4) ny = -1; // a hop in place
  const c = {
    x: Math.min(window.innerWidth - 24, Math.max(24, mid.x + nx * bend)),
    y: Math.min(window.innerHeight - 24, Math.max(24, mid.y + ny * bend - 30)),
  };
  const at = (s: number) => ({
    x: (1 - s) ** 2 * a.x + 2 * (1 - s) * s * c.x + s * s * b.x,
    y: (1 - s) ** 2 * a.y + 2 * (1 - s) * s * c.y + s * s * b.y,
  });
  const tangent = (s: number) => Math.atan2(2 * (1 - s) * (c.y - a.y) + 2 * s * (b.y - c.y), 2 * (1 - s) * (c.x - a.x) + 2 * s * (b.x - c.x));
  return { at, tangent, normal: { x: nx, y: ny } };
}

// The comet eases in and out along the arc; this is that easing and its inverse
// (when, in the flight, the comet is at a given point: where a print appears).
const ease = (u: number) => (1 - Math.cos(Math.PI * u)) / 2;
const when = (s: number) => Math.acos(1 - 2 * s) / Math.PI;

/** The comet itself, with its halo and trail, from `a` to `b`, starting `at` ms into the run. */
function fly(run: Run, a: Point, b: Point, species: Species | null, at: number, soft: boolean) {
  const path = arc(a, b);
  const sprite = comet(species);
  const head = place(run.layer, `fx-comet fx-${species ?? "unknown"}${soft ? " is-soft" : ""}`, a,
    `<span class="fx-halo"></span><span class="fx-sprite${b.x < a.x && species === "cat" ? " is-flipped" : ""}">${pixelSvg(sprite.grid, sprite.paint, 2.5)}</span>`);
  const frames: Keyframe[] = [];
  for (let i = 0; i <= 16; i++) {
    const u = i / 16, p = path.at(ease(u));
    const grow = u < 0.12 ? 0.3 + (u / 0.12) * 0.9 : u < 0.2 ? 1.2 - ((u - 0.12) / 0.08) * 0.2 : 1 - Math.max(0, u - 0.8) * 1.5;
    frames.push({ transform: `translate(${p.x - a.x}px,${p.y - a.y}px) scale(${grow.toFixed(3)})`, opacity: u > 0.94 ? 0 : 1, offset: u });
  }
  play(run, head, frames, FLIGHT_MS, at, "linear");
  const spin = head.querySelector(".fx-sprite")!;
  if (species === "dog") play(run, spin, [{ transform: "rotate(0deg)" }, { transform: "rotate(540deg)" }], FLIGHT_MS, at, "linear");
  else if (species === "cat") play(run, spin, [{ transform: "rotate(-14deg)" }, { transform: "rotate(14deg)" }, { transform: "rotate(-14deg)" }, { transform: "rotate(14deg)" }, { transform: "rotate(0deg)" }], FLIGHT_MS, at, "ease-in-out");
  else play(run, spin, [{ transform: "rotate(0deg) scale(1)" }, { transform: "rotate(90deg) scale(1.25)" }, { transform: "rotate(180deg) scale(1)" }], FLIGHT_MS, at, "linear");

  // The trail: prints left where the comet passed, alternating sides like footsteps, each its own colour.
  const grid = print(species);
  (soft ? PRINTS.filter((_, i) => i % 2 === 0) : PRINTS).forEach((s, i) => {
    const p = path.at(s), side = i % 2 ? 5 : -5;
    const angle = (path.tangent(s) * 180) / Math.PI + 90;
    const step = place(run.layer, "fx-print", { x: p.x + path.normal.x * side, y: p.y + path.normal.y * side },
      pixelSvg(grid, { P: `var(--fx-${(i % 5) + 1})` }, species ? 1.4 : 1.6));
    play(run, step, [
      { transform: `rotate(${angle}deg) scale(0.4)`, opacity: 0 },
      { transform: `rotate(${angle}deg) scale(1.1)`, opacity: soft ? 0.5 : 0.95, offset: 0.12 },
      { transform: `rotate(${angle}deg) scale(1)`, opacity: soft ? 0.4 : 0.8, offset: 0.4 },
      { transform: `rotate(${angle}deg) scale(0.9)`, opacity: 0 },
    ], 900, at + when(s) * FLIGHT_MS);
  });
}

/** The landing: a rotating multicolour ring around the target, and (for an open suggestion) what the animal says. */
function bloom(run: Run, target: Element, at: number, { soft = false, says = "" }: { soft?: boolean; says?: string }) {
  const r = target.getBoundingClientRect();
  const ring = place(run.layer, `fx-ring${soft ? " is-soft" : ""}`, { x: r.left - 4, y: r.top - 4 }, `<span class="fx-ring-spin"></span>`);
  const radius = parseFloat(getComputedStyle(target).borderTopLeftRadius) || 0;
  ring.style.cssText += `width:${r.width + 8}px;height:${r.height + 8}px;border-radius:${radius + 4}px`;
  const spinner = ring.firstElementChild as HTMLElement;
  const side = Math.hypot(r.width, r.height) + 16;
  spinner.style.cssText = `width:${side}px;height:${side}px;margin:${-side / 2}px 0 0 ${-side / 2}px`;
  play(run, ring, [
    { transform: "scale(0.9)", opacity: 0 },
    { transform: "scale(1.04)", opacity: 1, offset: 0.15 },
    { transform: "scale(1)", opacity: 1, offset: 0.65 },
    { transform: "scale(1.02)", opacity: 0 },
  ], BLOOM_MS, at);
  play(run, spinner, [{ transform: "rotate(0deg)" }, { transform: "rotate(360deg)" }], BLOOM_MS, at, "linear");
  if (!says) return;
  const bubble = place(run.layer, "fx-bubble", { x: r.right - 6, y: r.top - 4 });
  bubble.textContent = says;
  play(run, bubble, [
    { transform: "translate(0,-100%) scale(0.3)", opacity: 0 },
    { transform: "translate(0,-100%) scale(1.12)", opacity: 1, offset: 0.14 },
    { transform: "translate(0,-100%) scale(1)", opacity: 1, offset: 0.24 },
    { transform: "translate(0,-100%) scale(1)", opacity: 1, offset: 0.75 },
    { transform: "translate(0,calc(-100% - 8px)) scale(1)", opacity: 0 },
  ], BLOOM_MS + 200, at + 60);
}

/**
 * When (ms, at pace 1) the shimmer's brightest colour passes a point `f` of the way
 * across the transcript (0 its left edge, 1 its right). The band is upright, so every
 * line is crossed at once.
 */
export function shimmerCrossing(f: number): number {
  const p = (f - SHIMMER_GLOW * SHIMMER_WIDTH) / SHIMMER_WIDTH;
  const q = Math.min(1, Math.max(0, (p - SHIMMER_FROM) / (SHIMMER_TO - SHIMMER_FROM)));
  // CSS ease-in-out is cubic-bezier(0.42, 0, 0.58, 1): find the moment whose eased progress is q.
  const bezier = (a: number, b: number, t: number) => 3 * (1 - t) ** 2 * t * a + 3 * (1 - t) * t * t * b + t ** 3;
  let lo = 0, hi = 1;
  for (let i = 0; i < 30; i++) {
    const mid = (lo + hi) / 2;
    if (bezier(0, 1, mid) < q) lo = mid; else hi = mid;
  }
  return bezier(0.42, 0.58, lo) * SHIMMER_MS;
}

/** Where the shimmer crosses each phrase of an open suggestion that is on screen (ms, at pace 1), and whose phrase it is. */
function crossings(root: HTMLElement, open: Set<string>): { at: number; ids: string[] }[] {
  const out: { at: number; ids: string[] }[] = [];
  root.querySelectorAll<HTMLElement>(".transcript blockquote").forEach((quote) => {
    const box = quote.getBoundingClientRect();
    if (!box.width) return;
    quote.querySelectorAll("[data-fx]").forEach((mark) => {
      const ids = (mark.getAttribute("data-fx") ?? "").split(" ").filter((id) => open.has(id));
      const lines = [...mark.getClientRects()].filter((r) => r.width > 0 && inView(r));
      if (ids.length && lines.length) out.push({ at: Math.min(...lines.map((r) => shimmerCrossing((r.left + r.width / 2 - box.left) / box.width))), ids });
    });
  });
  return out;
}

/**
 * What is felt for a showing with `open` suggestions still open, the shimmer crossing
 * their phrases at `crossed` (ms): the scan's soft ticks, and each open beat's pulses
 * (ms from the beat's start, at pace 1). A beat's flight ticks are the ladder version of
 * `flight`, the ramp that phones with curves play instead.
 *
 * At most MAX_PULSES: every beat keeps its light-up and landing, a flight loses its
 * first tick before the scan is trimmed. A scan tick next to another, or next to a
 * phrase lighting up, is left out (the stronger is felt).
 */
export function suggestionHaptics(crossed: number[], open: number): { scan: number[]; beat: (i: number) => Pulse[]; flight: Ramp } {
  const lightUps = Array.from({ length: open }, (_, i) => FIRST_AT_MS + i * STEP_MS + LIGHT_AT_MS);
  let scan: number[] = [];
  for (const at of [...crossed].sort((a, b) => a - b)) {
    if (lightUps.some((lit) => Math.abs(lit - at) < MERGE_MS)) continue;
    if (scan.length && at - scan[scan.length - 1] < MERGE_MS) continue;
    scan.push(at);
  }
  scan = scan.slice(0, SCAN_TICKS === "once" ? 1 : MAX_SCAN_TICKS);
  const beats = Math.min(open, Math.floor(MAX_PULSES / 2));
  const room = MAX_PULSES - 2 * beats;
  const flight = beats * FLIGHT_TICKS.length + scan.length <= room
    ? FLIGHT_TICKS.length
    : Math.min(FLIGHT_TICKS.length - 1, Math.floor(room / Math.max(1, beats)));
  scan = scan.slice(0, Math.max(0, room - beats * flight));
  const skip = FLIGHT_TICKS.length - flight;
  return {
    scan,
    flight: FLIGHT_RAMP,
    beat: (i) => i >= beats ? [] : [
      { at: LIGHT_AT_MS, level: "light" },
      ...FLIGHT_TICKS.slice(skip).map((u, n) => ({ at: LAUNCH_AT_MS + u * FLIGHT_MS, level: FLIGHT_LEVELS[skip + n], ladderOnly: true })),
      { at: ARRIVE_MS, level: i === beats - 1 ? "success" : "heavy" },
    ],
  };
}

/**
 * The whole pattern for a made-up showing (four phrases, three suggestions, every comet
 * flying) at the current pace, as one score: for the Haptics check.
 */
export function sampleSuggestionScore(): { pulses: Pulse[]; ramps: Ramp[] } {
  const k = typeof document === "undefined" ? 1 : pace();
  const plan = suggestionHaptics([0.15, 0.4, 0.62, 0.85].map(shimmerCrossing), 3);
  const beats = [0, 1, 2].map((i) => FIRST_AT_MS + i * STEP_MS);
  return {
    pulses: [
      ...plan.scan.map((at) => ({ at, level: "soft" as Level })),
      ...beats.flatMap((begin, i) => plan.beat(i).map((pulse) => ({ ...pulse, at: begin + pulse.at }))),
    ].map((pulse) => ({ ...pulse, at: pulse.at * k })),
    ramps: beats.map((begin) => ({ ...plan.flight, at: (begin + plan.flight.at) * k, duration: plan.flight.duration * k })),
  };
}

/** The selector-safe form of a suggestion id. */
const esc = (id: string) => (typeof CSS !== "undefined" && CSS.escape ? CSS.escape(id) : id.replace(/["\\]/g, "\\$&"));

/** A suggestion to play: still open (flows to its chip) or used (flows into the field it filled, quieter). */
export interface FlowItem { id: string; used?: boolean }

/**
 * Play the whole sequence for these suggestions: the shimmer, then one beat per
 * suggestion, open ones first. An open one: its marked words light up and its
 * comet flies to the chip marked data-fx-chip="<id>", which blooms, says
 * something and keeps a paw stamp, and a tap is felt as it lands. A used one
 * (the suggestion was accepted, so what it suggested is already saved): the
 * same, softer and shorter, into the element marked data-fx-into="<id>" (the
 * field it filled), with no words and nothing felt, so reopening a sighting
 * doesn't buzz every time.
 *
 * With reduced motion nothing moves; one light tap says open suggestions are there.
 *
 * Each beat measures where things are when it starts, so a scroll in the
 * meantime doesn't send a comet to an old place. A chip or field that is
 * below the fold gets its pop when it is first seen instead of a comet.
 */
export function playSuggestionFlow(root: HTMLElement, items: FlowItem[]): () => void {
  if (!items.length) return () => {};
  if (!canAnimate()) {
    return reducedMotion() && items.some((item) => !item.used) ? playScore([{ at: 0, level: "light" }], "suggestions") : () => {};
  }
  const k = pace();
  const timers: ReturnType<typeof setTimeout>[] = [];
  const felt: (() => void)[] = [];
  const watching: IntersectionObserver[] = [];
  let run: Run | null = null;
  let pending = 0;
  const finish = (anims: Animation[]) => {
    void Promise.allSettled(anims.map((anim) => anim.finished)).then(() => { if (--pending === 0) run?.layer.remove(); });
  };
  root.querySelectorAll<HTMLElement>(".transcript blockquote").forEach((quote) => arm(quote, "data-fx-shimmer", 0));

  const ordered = [...items.filter((item) => !item.used), ...items.filter((item) => item.used)];
  const open = ordered.filter((item) => !item.used);
  // Still open: its chip, if it had one, is still that suggestion's chip. Answering can leave the
  // button on the page as a plain option (CaptureReview), so being on the page is not enough.
  const chips = new Map(open.map(({ id }) => [id, root.querySelector<HTMLElement>(`[data-fx-chip~="${esc(id)}"]`)]));
  const stillOpen = (id: string) => {
    const chip = chips.get(id);
    return !chip || (chip.isConnected && chip.matches(`[data-fx-chip~="${esc(id)}"]`));
  };
  const crossed = crossings(root, new Set(chips.keys()));
  const plan = suggestionHaptics(crossed.map((crossing) => crossing.at), open.length);
  felt.push(playScore(plan.scan.map((at) => ({
    at: at * k,
    level: "soft",
    still: () => crossed.some((crossing) => crossing.at === at && crossing.ids.some(stillOpen)),
  })), "suggestions scan"));
  let opened = 0;
  let start = FIRST_AT_MS;
  let said = 0;
  ordered.forEach(({ id, used = false }) => {
    const begin = start;
    const nth = used ? -1 : opened++;
    start += used ? USED_STEP_MS : STEP_MS;
    const target = root.querySelector<HTMLElement>(used ? `[data-fx-into~="${esc(id)}"]` : `[data-fx-chip~="${esc(id)}"]`);
    const species = speciesOf(target);
    const sound = !used && species ? SOUNDS[species][said++ % SOUNDS[species].length] : "";
    const [attr, value] = used ? ["data-fx-fill", "soft"] : ["data-fx-play", ""];
    if (target && !inView(target.getBoundingClientRect()) && typeof IntersectionObserver !== "undefined") {
      // Below the fold (the details sheet): pop it, once, when it is first seen.
      const watcher = new IntersectionObserver((entries) => {
        if (!entries.some((entry) => entry.isIntersecting)) return;
        watcher.disconnect();
        arm(target, attr, 0, value);
      });
      watcher.observe(target);
      watching.push(watcher);
    } else if (target) arm(target, attr, (begin + ARRIVE_MS) * k, value);
    pending++;
    timers.push(setTimeout(() => {
      run ??= { layer: makeLayer(), k, anims: [] };
      run.anims = [];
      const mark = root.querySelector<HTMLElement>(`[data-fx~="${esc(id)}"]`);
      const words = mark ? snapshot(mark) : null;
      const lit = !!words?.items.length && words.items.some((word) => inView(word.rect));
      const to = target?.isConnected ? target.getBoundingClientRect() : null;
      const flies = lit && !!to && inView(to);
      if (lit) {
        // The comet is born in the middle of the phrase's first visible line.
        const visible = words!.items.filter((word) => inView(word.rect));
        const line = visible.filter((word) => Math.abs(word.rect.top - visible[0].rect.top) < 2);
        const from = { x: (line[0].rect.left + line[line.length - 1].rect.right) / 2, y: centre(line[0].rect).y };
        lightUp(run, words!, flies ? from : null, used);
        if (flies) {
          fly(run, from, centre(to!), species, LAUNCH_AT_MS, used);
          bloom(run, target!, ARRIVE_MS, { soft: used, says: sound });
        }
        // Felt only for a suggestion still open: its light-up, and the flight and landing
        // only if the comet really flies. Nothing more once it has been answered.
        if (!used) {
          const pulses = plan.beat(nth).filter((_, i) => flies || i === 0);
          const flight = flies && pulses.length ? [{ ...plan.flight, at: plan.flight.at * k, duration: plan.flight.duration * k, still: () => stillOpen(id) }] : [];
          felt.push(playScore(pulses.map((pulse) => ({ ...pulse, at: pulse.at * k, still: () => stillOpen(id) })), "suggestion", flight));
        }
      }
      finish(run.anims);
    }, begin * k));
  });
  return () => {
    timers.forEach(clearTimeout);
    felt.forEach((stop) => stop());
    watching.forEach((watcher) => watcher.disconnect());
    run?.layer.getAnimations?.({ subtree: true }).forEach((anim) => anim.cancel());
    run?.layer.remove();
  };
}

/**
 * Using a suggestion: a comet hops from the chip into the field, which blooms.
 * The chip is measured now, but the field only after the caller's state update
 * has committed: answering the suggestion removes its row, and the fields
 * below it move up, so a destination measured earlier would be the old place.
 * Without a chip (the chip is the field), it just blooms.
 */
export function playFill(chip: Element | null, field: HTMLElement | null): void {
  if (!field || !canAnimate()) return;
  const from = chip ? centre(chip.getBoundingClientRect()) : null;
  requestAnimationFrame(() => {
    const run: Run = { layer: makeLayer(), k: pace(), anims: [] };
    const hop = from && inView(field.getBoundingClientRect()) ? LAUNCH_AT_MS : 0;
    if (hop) fly(run, from!, centre(field.getBoundingClientRect()), speciesOf(field), 0, false);
    bloom(run, field, hop ? FLIGHT_MS : 0, {});
    arm(field, "data-fx-fill", (hop ? FLIGHT_MS : 0) * run.k);
    void Promise.allSettled(run.anims.map((anim) => anim.finished)).then(() => run.layer.remove());
  });
}

/**
 * Play the sequence inside `ref` every time it is shown: the scope is mounted
 * per entry, so each opening plays again. Within one showing an id plays once
 * (answering a suggestion doesn't replay the rest). `ids` are the open
 * suggestions, `usedIds` the used ones. With reduced motion it does nothing,
 * and the resting state (with its paw stamps) shows through.
 */
export function useSuggestionFlow(ref: RefObject<HTMLElement>, ids: string[], usedIds: string[] = []) {
  const seen = useRef(new Set<string>());
  const cancel = useRef<() => void>(() => {});
  const fresh: FlowItem[] = [...ids.map((id) => ({ id })), ...usedIds.map((id) => ({ id, used: true }))]
    .filter(({ id }) => !seen.current.has(id));
  useLayoutEffect(() => {
    if (!fresh.length || !ref.current) return;
    fresh.forEach(({ id }) => seen.current.add(id));
    cancel.current();
    cancel.current = playSuggestionFlow(ref.current, fresh);
  });
  // Unmounted (or the dev-only strict remount): stop, and forget, so a real
  // remount plays again.
  useEffect(() => () => { cancel.current(); seen.current.clear(); }, []);
}
