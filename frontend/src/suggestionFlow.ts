import "./suggestionFlow.css";
import { haptic } from "./haptics";
import { useEffect, useLayoutEffect, useRef, type RefObject } from "react";

/**
 * The "magic" around voice-note suggestions (#118): the words a suggestion was
 * read from glow in the transcript, lift off and flow into the chip they
 * produced, and the chip glows; using a suggestion flows it into its field.
 *
 * Done with CSS keyframes (the glows, in styles.css, keyed off data-fx-play /
 * data-fx-fill) and the Web Animations API (the flight, which needs measured
 * positions). Only transform and opacity are animated for the flight, so it
 * stays on the compositor. Everything plays once and ends in the normal
 * resting state; nothing here changes layout, takes focus or catches a tap.
 * Decoration only: the marks, chips and field values carry the meaning.
 */

const STAGGER_MS = 160;      // between one suggestion and the next
const LIFT_AT_MS = 450;      // words start leaving once the glow has built
const FLIGHT_MS = 800;
const MAX_WORDS = 8;         // a long quote still flies, but briefly
export const ARRIVE_MS = LIFT_AT_MS + FLIGHT_MS - 150; // the chip lights as the words land

export const reducedMotion = () =>
  typeof window !== "undefined" && typeof window.matchMedia === "function" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const canAnimate = () => !reducedMotion() && typeof Element !== "undefined" && typeof Element.prototype.animate === "function";

/** Re-arm a CSS animation that is keyed off a data attribute, with a delay. */
function arm(el: HTMLElement, attr: string, delayMs: number) {
  el.style.setProperty("--fx-delay", `${delayMs}ms`);
  el.removeAttribute(attr);
  void el.offsetWidth; // restart if it was already playing
  el.setAttribute(attr, "");
}

function wordRects(el: Element): { text: string; rect: DOMRect }[] {
  const out: { text: string; rect: DOMRect }[] = [];
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    for (const m of (node.textContent ?? "").matchAll(/\S+/g)) {
      const range = document.createRange();
      range.setStart(node, m.index!);
      range.setEnd(node, m.index! + m[0].length);
      const rect = range.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) out.push({ text: m[0], rect });
    }
  }
  return out;
}

const inView = (r: DOMRect) => r.bottom > 0 && r.top < window.innerHeight && r.right > 0 && r.left < window.innerWidth;

interface Words { items: { text: string; rect: DOMRect }[]; font: string }

/** Where a source's words are right now, so they can be flown after the layout has moved on. */
function snapshot(from: Element): Words {
  return { items: wordRects(from).slice(0, MAX_WORDS), font: getComputedStyle(from).font };
}

/**
 * Carry the words of a source to the centre of `to`: each word is a throwaway
 * copy placed over the original (FLIP: first rect measured earlier, last rect
 * is measured here, where it lands) and animated with transform + opacity
 * only. Skipped when either end is off screen: a flight to nowhere is just noise.
 */
function flowWords({ items, font }: Words, to: Element, delayMs: number, layer: HTMLElement): Promise<unknown>[] {
  const target = to.getBoundingClientRect();
  if (!items.length || !inView(target) || !items.some((w) => inView(w.rect))) return [];
  const cx = target.left + target.width / 2;
  const cy = target.top + target.height / 2;
  return items.map(({ text, rect }, i) => {
    const ghost = document.createElement("span");
    ghost.className = "fx-word";
    ghost.textContent = text;
    ghost.style.cssText = `left:${rect.left}px;top:${rect.top}px;width:${rect.width}px;height:${rect.height}px;` +
      `font:${font};line-height:${rect.height}px`;
    layer.appendChild(ghost);
    const dx = cx - (rect.left + rect.width / 2);
    const dy = cy - (rect.top + rect.height / 2);
    const scale = Math.min(1, Math.max(0.35, target.width / Math.max(rect.width, 1) / 2));
    return ghost.animate([
      { transform: "translate(0,0) scale(1)", opacity: 0 },
      { transform: "translate(0,0) scale(1.06)", opacity: 1, offset: 0.15 },
      { transform: `translate(${dx * 0.45}px,${dy * 0.45 - 10}px) scale(1)`, opacity: 1, offset: 0.55 },
      { transform: `translate(${dx}px,${dy}px) scale(${scale})`, opacity: 0 },
    ], { duration: FLIGHT_MS, delay: delayMs + i * 45, easing: "cubic-bezier(0.4, 0, 0.2, 1)", fill: "both" }).finished;
  });
}

/** Throwaway fixed layer for the flying words: never catches a tap. */
function withLayer(run: (layer: HTMLElement) => Promise<unknown>[]): () => void {
  const layer = document.createElement("div");
  layer.className = "fx-layer";
  layer.setAttribute("aria-hidden", "true");
  document.body.appendChild(layer);
  const flights = run(layer);
  const remove = () => layer.remove();
  if (!flights.length) remove();
  else void Promise.allSettled(flights).then(remove);
  return () => { layer.getAnimations?.({ subtree: true }).forEach((a) => a.cancel()); remove(); };
}

/** The selector-safe form of a suggestion id. */
const esc = (id: string) => (typeof CSS !== "undefined" && CSS.escape ? CSS.escape(id) : id.replace(/["\\]/g, "\\$&"));

/**
 * Play the whole sequence for these suggestions: marked words glow, fly to the
 * chip marked data-fx-chip="<id>", and the chip glows on arrival.
 */
export function playSuggestionFlow(root: HTMLElement, ids: string[]): () => void {
  if (!canAnimate()) return () => {};
  const flights: { mark: Element | null; chip: HTMLElement; delay: number }[] = [];
  const watching: IntersectionObserver[] = [];
  ids.forEach((id, i) => {
    const delay = i * STAGGER_MS;
    const mark = root.querySelector<HTMLElement>(`[data-fx~="${esc(id)}"]`);
    const chip = root.querySelector<HTMLElement>(`[data-fx-chip~="${esc(id)}"]`);
    if (mark) arm(mark, "data-fx-play", delay);
    if (!chip) return;
    if (inView(chip.getBoundingClientRect()) || typeof IntersectionObserver === "undefined") {
      arm(chip, "data-fx-play", delay + ARRIVE_MS);
      flights.push({ mark, chip, delay });
    } else {
      // Below the fold (the details sheet): light it, once, when it is first seen.
      const watcher = new IntersectionObserver((entries) => {
        if (!entries.some((entry) => entry.isIntersecting)) return;
        watcher.disconnect();
        arm(chip, "data-fx-play", 0);
      });
      watcher.observe(chip);
      watching.push(watcher);
    }
  });
  const aloft = flights.filter((f) => f.mark);
  const ticks: ReturnType<typeof setTimeout>[] = [];
  const stopFlight = aloft.length
    ? withLayer((layer) => aloft.flatMap(({ mark, chip, delay }) => {
      const flying = flowWords(snapshot(mark!), chip, delay + LIFT_AT_MS, layer);
      // A tap as the words land in the chip, with the chip's ring; only for a flight that really happens.
      if (flying.length) ticks.push(setTimeout(() => haptic("tick"), delay + ARRIVE_MS));
      return flying;
    }))
    : () => {};
  return () => { stopFlight(); ticks.forEach(clearTimeout); watching.forEach((watcher) => watcher.disconnect()); };
}

/**
 * Using a suggestion: its words flow from the chip into the field, which glows.
 * The chip is measured now, but the field only after the caller's state update
 * has committed: answering the suggestion removes its row, and the fields
 * below it move up, so a destination measured earlier would be the old place.
 */
export function playFill(chip: Element | null, field: HTMLElement | null): void {
  if (!field || !canAnimate()) return;
  const from = chip ? snapshot(chip) : null;
  requestAnimationFrame(() => {
    arm(field, "data-fx-fill", LIFT_AT_MS + FLIGHT_MS - 250);
    if (from) withLayer((layer) => flowWords(from, field, 0, layer));
  });
}

/**
 * Play the sequence inside `ref` when suggestions appear: once for each id the
 * first time it is seen, never again (answering one doesn't replay the rest).
 * With reduced motion it does nothing, and the resting state shows through.
 */
export function useSuggestionFlow(ref: RefObject<HTMLElement>, ids: string[]) {
  const seen = useRef(new Set<string>());
  const cancel = useRef<() => void>(() => {});
  const fresh = ids.filter((id) => !seen.current.has(id));
  useLayoutEffect(() => {
    if (!fresh.length || !ref.current) return;
    fresh.forEach((id) => seen.current.add(id));
    cancel.current();
    cancel.current = playSuggestionFlow(ref.current, fresh);
  });
  // Unmounted (or the dev-only strict remount): stop, and forget, so a real
  // remount plays again.
  useEffect(() => () => { cancel.current(); seen.current.clear(); }, []);
}
