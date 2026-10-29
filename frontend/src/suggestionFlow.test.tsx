// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import SuggestionFlow from "./components/SuggestionFlow";
import Transcript from "./components/Transcript";
import {
  ARRIVE_MS, FIRST_AT_MS, MAX_PULSES, playFill, sampleSuggestionScore, SHIMMER_FROM, SHIMMER_MS, SHIMMER_TO, SHIMMER_WIDTH,
  shimmerCrossing, STEP_MS, suggestionHaptics, USED_STEP_MS,
} from "./suggestionFlow";
import { readFileSync } from "node:fs";
import { BONE, CAT_PAW, DOG_PAW, FISH, fxScope, pixelSvg, SPARKLE, TWINKLE } from "./fxSprites";
import type { Pulse, Ramp } from "./haptics";

// What would be felt: each pulse of a score as it plays (its level, and when), unless its last check says no.
const felt = vi.hoisted(() => [] as { level: string; at: number }[]);
const flights = vi.hoisted(() => [] as Ramp[][]);
vi.mock("./haptics", () => ({
  playScore: (pulses: Pulse[], _label: string, ramps: Ramp[] = []) => {
    flights.push(ramps);
    const timers = pulses.map((pulse) => setTimeout(() => {
      if (!pulse.still || pulse.still()) felt.push({ level: pulse.level, at: Date.now() }); // the clock starts at 0 in each test
    }, pulse.at));
    return () => timers.forEach(clearTimeout);
  },
}));
const levels = () => felt.map((pulse) => pulse.level);

const text = "She is a female. Her name is Kaalu.";
const highlights = [
  { id: "sex", quote: "She is a female", label: "Sex: female" },
  { id: "name", quote: "Her name is Kaalu", label: "Name: Kaalu" },
];
const rect = { left: 10, top: 10, right: 60, bottom: 30, width: 50, height: 20, x: 10, y: 10, toJSON() {} } as DOMRect;

function scene(ids: string[], usedIds: string[] = [], species: string | null = "dog") {
  return <SuggestionFlow ids={ids} usedIds={usedIds} species={species}>
    <Transcript transcripts={[{ source: "note", language: "en", text }]} highlights={highlights} />
    <button className="chip suggested" data-fx-chip="sex">female</button>
    <button className="chip suggested" data-fx-chip="name">Kaalu</button>
    <span data-testid="filled-name" data-fx-into="name">Kaalu</span>
  </SuggestionFlow>;
}

const layer = () => document.querySelector<HTMLElement>(".fx-layer");
const delayOf = (el: HTMLElement) => el.style.getPropertyValue("--fx-delay");

let animate: ReturnType<typeof vi.fn>;
beforeEach(() => {
  vi.useFakeTimers({ now: 0 });
  animate = vi.fn(() => ({ finished: Promise.resolve(), cancel() {} }));
  Element.prototype.animate = animate as never;
  Range.prototype.getBoundingClientRect = () => rect;
  Element.prototype.getBoundingClientRect = () => rect;
  Element.prototype.getClientRects = function () { return [this.getBoundingClientRect()] as never; }; // jsdom lays nothing out
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  felt.length = 0;
  flights.length = 0;
  vi.unstubAllGlobals();
  document.documentElement.style.removeProperty("--fx-pace");
  delete (Element.prototype as { animate?: unknown }).animate;
});

describe("the shimmer and comets around suggestions", () => {
  it("shimmers the transcript at once, then plays one suggestion at a time", async () => {
    const { container } = render(scene(["sex", "name"]));
    expect(container.querySelector("blockquote")).toHaveAttribute("data-fx-shimmer");
    expect(animate).not.toHaveBeenCalled(); // nothing flies before the first beat
    // Each chip pops as its comet lands, one beat after the other.
    expect(delayOf(screen.getByRole("button", { name: "female" }))).toBe(`${FIRST_AT_MS + ARRIVE_MS}ms`);
    expect(delayOf(screen.getByRole("button", { name: "Kaalu" }))).toBe(`${FIRST_AT_MS + STEP_MS + ARRIVE_MS}ms`);
    act(() => { vi.advanceTimersByTime(FIRST_AT_MS); });
    expect(layer()!.querySelectorAll(".fx-word")).toHaveLength(4); // "She is a female" lights up
    expect(layer()!.querySelectorAll(".fx-comet")).toHaveLength(1);
    expect(layer()!.querySelector(".fx-ring")).not.toBeNull();
    act(() => { vi.advanceTimersByTime(STEP_MS - 1); });
    expect(layer()!.querySelectorAll(".fx-comet")).toHaveLength(1); // the second waits for its beat
    act(() => { vi.advanceTimersByTime(1); });
    expect(layer()!.querySelectorAll(".fx-comet")).toHaveLength(2);
  });

  it("draws everything on a layer that takes no tap, hides from assistive tech, and cleans up after itself", async () => {
    render(scene(["sex"]));
    act(() => { vi.advanceTimersByTime(FIRST_AT_MS); });
    expect(layer()).toHaveAttribute("aria-hidden", "true");
    expect(layer()).toHaveClass("fx-layer");
    await act(async () => { await vi.runAllTimersAsync(); });
    expect(layer()).toBeNull();
  });

  it("themes the comet, the trail and what is said by species", () => {
    render(scene(["sex", "name"], [], "cat"));
    act(() => { vi.advanceTimersByTime(FIRST_AT_MS + STEP_MS); });
    expect(layer()!.querySelectorAll(".fx-comet.fx-cat")).toHaveLength(2);
    expect(layer()!.querySelectorAll(".fx-print")).toHaveLength(10);
    expect([...layer()!.querySelectorAll(".fx-bubble")].map((b) => b.textContent)).toEqual(["mrrp", "prrt"]);
    cleanup();
    render(scene(["sex"], [], "dog"));
    act(() => { vi.advanceTimersByTime(FIRST_AT_MS); });
    expect(layer()!.querySelector(".fx-comet")).toHaveClass("fx-dog");
    expect(layer()!.querySelector(".fx-bubble")).toHaveTextContent("woof");
  });

  it("gives an animal of unknown species a sparkle, and no words", () => {
    render(scene(["sex"], [], null));
    act(() => { vi.advanceTimersByTime(FIRST_AT_MS); });
    expect(layer()!.querySelector(".fx-comet")).toHaveClass("fx-unknown");
    expect(layer()!.querySelector(".fx-bubble")).toBeNull();
  });

  it("is felt building with the animation: the scan, each light-up, a flight getting firmer, a heavy landing, a success last", () => {
    render(scene(["sex", "name"]));
    vi.advanceTimersByTime(10_000);
    expect(levels()).toEqual([
      "soft", // the shimmer crosses the phrases (both in one place here, so one tick)
      "light", "light", "medium", "firm", "heavy",
      "light", "light", "medium", "firm", "success",
    ]);
    const at = (i: number) => felt[i].at;
    expect(at(0)).toBe(Math.floor(shimmerCrossing(0.5)));
    expect(at(5)).toBe(FIRST_AT_MS + ARRIVE_MS); // in step with the first ring
    expect(at(10)).toBe(FIRST_AT_MS + STEP_MS + ARRIVE_MS);
    // The flight's ticks come closer and closer, up to the landing.
    const gaps = [at(2) - at(1), at(3) - at(2), at(4) - at(3), at(5) - at(4)].slice(1);
    expect(gaps[0]).toBeGreaterThan(gaps[1]);
    expect(gaps[1]).toBeGreaterThan(gaps[2]);
  });

  it("ticks the scan once per open phrase, where the shimmer crosses it, and never for a used one", () => {
    Element.prototype.getBoundingClientRect = function () {
      if (this.tagName === "BLOCKQUOTE") return { ...rect, left: 0, right: 200, width: 200 } as DOMRect;
      if (this.getAttribute("data-fx") === "sex") return { ...rect, left: 0, right: 40, width: 40 } as DOMRect; // a tenth of the way
      if (this.getAttribute("data-fx") === "name") return { ...rect, left: 70, right: 130, width: 60 } as DOMRect; // halfway
      return rect;
    };
    const scan = () => felt.filter((pulse) => pulse.level === "soft").map((pulse) => pulse.at);
    render(scene(["sex", "name"]));
    vi.advanceTimersByTime(SHIMMER_MS);
    expect(scan()).toEqual([Math.floor(shimmerCrossing(0.1)), Math.floor(shimmerCrossing(0.5))]);
    cleanup(); felt.length = 0;
    vi.setSystemTime(0);
    render(scene(["sex"], ["name"]));
    vi.advanceTimersByTime(SHIMMER_MS);
    expect(scan()).toEqual([Math.floor(shimmerCrossing(0.1))]);
  });

  it("stops feeling a flight whose chip was answered and removed on the way", () => {
    render(scene(["sex", "name"]));
    vi.advanceTimersByTime(FIRST_AT_MS + ARRIVE_MS - 200);
    screen.getByRole("button", { name: "female" }).remove(); // answered; the scope stays mounted
    vi.advanceTimersByTime(5000);
    expect(levels()).toEqual(["soft", "light", "light", "medium", /* removed: no more for it */ "light", "light", "medium", "firm", "success"]);
  });

  it("stops feeling a suggestion answered while its button stays on the page (the review sheet keeps it as a plain option)", () => {
    render(scene(["sex", "name"]));
    vi.advanceTimersByTime(FIRST_AT_MS + ARRIVE_MS - 200);
    screen.getByRole("button", { name: "female" }).removeAttribute("data-fx-chip"); // accepted or "not this": still mounted
    vi.advanceTimersByTime(5000);
    expect(levels()).toEqual(["soft", "light", "light", "medium", "light", "light", "medium", "firm", "success"]);
  });

  it("leaves out a scan tick for a phrase whose suggestion was answered before the shimmer got there", () => {
    Element.prototype.getBoundingClientRect = function () {
      if (this.tagName === "BLOCKQUOTE") return { ...rect, left: 0, right: 200, width: 200 } as DOMRect;
      if (this.getAttribute("data-fx") === "sex") return { ...rect, left: 0, right: 40, width: 40 } as DOMRect;
      if (this.getAttribute("data-fx") === "name") return { ...rect, left: 70, right: 130, width: 60 } as DOMRect;
      return rect;
    };
    render(scene(["sex", "name"]));
    screen.getByRole("button", { name: "Kaalu" }).removeAttribute("data-fx-chip");
    vi.advanceTimersByTime(SHIMMER_MS);
    expect(felt.filter((pulse) => pulse.level === "soft").map((pulse) => pulse.at)).toEqual([Math.floor(shimmerCrossing(0.1))]);
  });

  it("hands each flight over as one smooth build too, for phones that can play it instead of the ticks", () => {
    render(scene(["sex"]));
    vi.advanceTimersByTime(FIRST_AT_MS);
    expect(flights[flights.length - 1]).toEqual([{ at: ARRIVE_MS - 700, duration: 700, from: "light", to: "heavy", still: expect.any(Function) }]);
  });

  it("lights up, but feels no flight or landing, for a chip it cannot fly to", () => {
    Element.prototype.getBoundingClientRect = function () {
      return this.getAttribute("data-fx-chip") === "sex" ? { ...rect, top: 5000, bottom: 5020 } as DOMRect : rect;
    };
    render(scene(["sex"]));
    vi.advanceTimersByTime(5000);
    expect(levels()).toEqual(["soft", "light"]);
    expect(flights.flat()).toEqual([]);
  });

  it("measures each beat when it starts, so a later comet goes where its chip is now", () => {
    render(scene(["sex", "name"]));
    const chip = screen.getByRole("button", { name: "Kaalu" });
    chip.getBoundingClientRect = () => ({ ...rect, left: 200, top: 300, right: 250, bottom: 320 }) as DOMRect;
    act(() => { vi.advanceTimersByTime(FIRST_AT_MS + STEP_MS); });
    const rings = [...layer()!.querySelectorAll<HTMLElement>(".fx-ring")];
    expect(rings[1].style.left).toBe("196px");
    expect(rings[1].style.top).toBe("296px");
  });

  it("replays a used suggestion too: softer, quicker, into the field it filled, wordless and silent", () => {
    render(scene([], ["name"]));
    const field = screen.getByTestId("filled-name");
    expect(field).toHaveAttribute("data-fx-fill", "soft");
    expect(delayOf(field)).toBe(`${FIRST_AT_MS + ARRIVE_MS}ms`);
    act(() => { vi.advanceTimersByTime(FIRST_AT_MS); });
    expect(layer()!.querySelector(".fx-comet")).toHaveClass("is-soft");
    expect(layer()!.querySelectorAll(".fx-word.is-soft")).toHaveLength(4);
    expect(layer()!.querySelector(".fx-bubble")).toBeNull();
    vi.advanceTimersByTime(5000);
    expect(felt).toEqual([]); // reopening a sighting must not buzz every time
  });

  it("plays open suggestions first, then used ones on a shorter beat", () => {
    render(scene(["sex"], ["name"]));
    expect(delayOf(screen.getByTestId("filled-name"))).toBe(`${FIRST_AT_MS + STEP_MS + ARRIVE_MS}ms`);
    vi.advanceTimersByTime(FIRST_AT_MS + STEP_MS + USED_STEP_MS + 3000);
    expect(levels()).toEqual(["soft", "light", "light", "medium", "firm", "success"]); // only the open one, and its landing is the last
  });

  it("takes all its timing from one speed setting, --fx-pace", () => {
    document.documentElement.style.setProperty("--fx-pace", "2");
    render(scene(["sex", "name"]));
    expect(delayOf(screen.getByRole("button", { name: "Kaalu" }))).toBe(`${2 * (FIRST_AT_MS + STEP_MS + ARRIVE_MS)}ms`);
    vi.advanceTimersByTime(10_000);
    expect(felt[0].at).toBe(Math.floor(2 * shimmerCrossing(0.5)));
    expect(felt.find((pulse) => pulse.level === "heavy")!.at).toBe(2 * (FIRST_AT_MS + ARRIVE_MS));
    expect(felt.find((pulse) => pulse.level === "success")!.at).toBe(2 * (FIRST_AT_MS + STEP_MS + ARRIVE_MS));
    const durations = animate.mock.calls.map((call) => (call[1] as { duration: number }).duration);
    expect(Math.min(...durations)).toBeGreaterThanOrEqual(2 * 700);
  });

  it("plays again every time it is shown, not just the first", () => {
    const first = render(scene(["sex"], ["name"]));
    act(() => { vi.advanceTimersByTime(FIRST_AT_MS); });
    expect(animate).toHaveBeenCalled();
    first.unmount();
    expect(layer()).toBeNull(); // closing mid-way stops it
    animate.mockClear();
    render(scene(["sex"], ["name"]));
    act(() => { vi.advanceTimersByTime(FIRST_AT_MS); });
    expect(animate).toHaveBeenCalled();
  });

  it("plays once: answering one suggestion does not replay the rest", () => {
    const { rerender } = render(scene(["sex", "name"]));
    act(() => { vi.runAllTimers(); });
    animate.mockClear();
    rerender(scene(["name"]));
    rerender(scene(["name", "sex"]));
    act(() => { vi.runAllTimers(); });
    expect(animate).not.toHaveBeenCalled();
  });

  it("holds back a chip that is below the fold until it is first seen, then pops it once", () => {
    let seen: (entries: { isIntersecting: boolean }[]) => void = () => {};
    const disconnect = vi.fn();
    vi.stubGlobal("IntersectionObserver", class { constructor(cb: typeof seen) { seen = cb; } observe() {} disconnect = disconnect; });
    Element.prototype.getBoundingClientRect = function () {
      return this.getAttribute("data-fx-chip") === "name" ? { ...rect, top: 5000, bottom: 5020 } as DOMRect : rect;
    };
    render(scene(["name"]));
    const chip = screen.getByRole("button", { name: "Kaalu" });
    expect(chip).not.toHaveAttribute("data-fx-play");
    act(() => { vi.advanceTimersByTime(FIRST_AT_MS); });
    expect(layer()!.querySelector(".fx-comet")).toBeNull(); // no comet to somewhere you can't see
    expect(layer()!.querySelectorAll(".fx-word")).toHaveLength(4); // but the words still light up
    act(() => seen([{ isIntersecting: true }]));
    expect(chip).toHaveAttribute("data-fx-play");
    expect(delayOf(chip)).toBe("0ms");
    expect(disconnect).toHaveBeenCalled();
  });

  it("moves nothing under reduced motion: the marks, chips and their paw stamps are simply there, with one light tap", () => {
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: query.includes("reduce"), media: query }));
    const { container } = render(scene(["sex", "name"]));
    vi.runAllTimers();
    expect(animate).not.toHaveBeenCalled();
    expect(felt).toEqual([{ level: "light", at: 0 }]); // no scan, flight or landing: one tap says they are there
    expect(container.querySelector("[data-fx-play], [data-fx-shimmer]")).toBeNull();
    expect(layer()).toBeNull();
    expect(container.querySelectorAll("mark")).toHaveLength(2);
    // The stamp is the resting state, drawn by CSS from the scope.
    const scope = container.querySelector<HTMLElement>(".fx-scope")!;
    expect(scope).toHaveAttribute("data-fx-species", "dog");
    expect(scope.style.getPropertyValue("--fx-stamp")).toContain("data:image/svg+xml");
  });

  it("flies into a used suggestion's field where it is after the layout has moved, and blooms it", () => {
    // Answering a suggestion removes its row, so the field below moves up.
    vi.useRealTimers();
    const frames: (() => void)[] = [];
    vi.stubGlobal("requestAnimationFrame", (cb: () => void) => frames.push(cb));
    const chip = document.createElement("strong");
    chip.textContent = "female";
    const field = document.createElement("select");
    document.body.append(chip, field);
    let moved = false;
    field.getBoundingClientRect = () => ({ ...rect, left: 100, top: moved ? 100 : 500, width: 40, height: 20 }) as DOMRect;
    playFill(chip, field);
    moved = true;
    expect(animate).not.toHaveBeenCalled();
    frames.forEach((frame) => frame());
    expect(field).toHaveAttribute("data-fx-fill");
    // The comet lands on the field's new centre (120, 110), not the old one (120, 510): from the chip's (35, 20).
    const keyframes = animate.mock.calls[0][0] as { transform: string }[];
    expect(keyframes[keyframes.length - 1].transform).toContain("translate(85px,90px)");
    expect(document.querySelector(".fx-ring")).not.toBeNull();
    chip.remove(); field.remove(); document.querySelector(".fx-layer")?.remove();
  });
});

describe("what is felt", () => {
  it("follows the shimmer as the CSS draws it", () => {
    const css = readFileSync(`${__dirname}/suggestionFlow.css`, "utf8");
    expect(css).toContain(`animation: fx-shimmer calc(var(--fx-pace) * ${SHIMMER_MS}ms) ease-in-out`);
    expect(css).toMatch(new RegExp(`\\[data-fx-shimmer\\]::after \\{[^}]*width: ${Math.round(SHIMMER_WIDTH * 100)}%`));
    const shimmer = /@keyframes fx-shimmer \{([^@]*)\n\}/.exec(css)![1];
    expect(shimmer).toContain(`0% { transform: translateX(${Math.round(SHIMMER_FROM * 100)}%)`);
    expect(shimmer).toContain(`100% { transform: translateX(${Math.round(SHIMMER_TO * 100)}%)`);
  });

  it("crosses the transcript from left to right while the shimmer runs", () => {
    const times = [0, 0.25, 0.5, 0.75, 1].map(shimmerCrossing);
    expect(times).toEqual([...times].sort((a, b) => a - b));
    expect(times[0]).toBeGreaterThan(0);
    expect(times[4]).toBeLessThan(SHIMMER_MS);
  });

  it("never plays more than the cap: flights lose a tick, then the scan thins, but every light-up and landing stays", () => {
    const count = (open: number, scan: number) => {
      const plan = suggestionHaptics(Array.from({ length: scan }, (_, i) => 100 + i * 80), open);
      const beats = Array.from({ length: open }, (_, i) => plan.beat(i));
      return { scan: plan.scan.length, flight: beats[0].length - 2, total: plan.scan.length + beats.flat().length, beats };
    };
    expect(count(3, 6)).toMatchObject({ scan: 6, flight: 3, total: 21 });
    expect(count(5, 6)).toMatchObject({ scan: 4, flight: 2, total: MAX_PULSES });
    expect(count(5, 6).beats[0].map((pulse) => pulse.level)).toEqual(["light", "medium", "firm", "heavy"]); // the softest tick goes first
    expect(count(6, 6)).toMatchObject({ scan: 0, flight: 2, total: MAX_PULSES });
    expect(count(20, 6).total).toBeLessThanOrEqual(MAX_PULSES);
    expect(count(20, 6).beats[19]).toEqual([]);
  });

  it("leaves out a scan tick that would blur into another or into a phrase lighting up", () => {
    const firstLit = suggestionHaptics([], 1).beat(0)[0].at + FIRST_AT_MS;
    expect(suggestionHaptics([400, 430, firstLit - 30, 600], 1).scan).toEqual([400, 600]);
  });

  it("offers the whole pattern to the Haptics check, at the current pace", () => {
    const { pulses, ramps } = sampleSuggestionScore();
    expect(pulses.filter((pulse) => pulse.level === "soft").length).toBeGreaterThan(0);
    expect(pulses.filter((pulse) => pulse.level === "heavy")).toHaveLength(2);
    expect(pulses[pulses.length - 1]).toEqual({ level: "success", at: FIRST_AT_MS + 2 * STEP_MS + ARRIVE_MS });
    expect(ramps.map((ramp) => ramp.at + ramp.duration)).toEqual([0, 1, 2].map((i) => FIRST_AT_MS + i * STEP_MS + ARRIVE_MS)); // each build ends on its landing
    document.documentElement.style.setProperty("--fx-pace", "2");
    const slow = sampleSuggestionScore();
    expect(slow.pulses[pulses.length - 1].at).toBe(2 * (FIRST_AT_MS + 2 * STEP_MS + ARRIVE_MS));
    expect(slow.ramps[0].duration).toBe(2 * ramps[0].duration);
  });
});

describe("the sprites", () => {
  it("are rectangular grids with a paint for every pixel they use", () => {
    for (const { grid, paint } of [BONE, FISH, SPARKLE]) {
      expect(new Set(grid.map((row) => row.length)).size).toBe(1);
      expect([...grid.join("")].filter((ch) => ch !== "." && ch !== " " && !paint[ch])).toEqual([]);
    }
    for (const grid of [CAT_PAW, DOG_PAW, TWINKLE]) {
      expect(new Set(grid.map((row) => row.length)).size).toBe(1);
      expect([...grid.join("")].every((ch) => ch === "P" || ch === ".")).toBe(true);
    }
  });

  it("draw a dog's print with claw marks and a cat's without", () => {
    expect(DOG_PAW.slice(0, 2).join("")).toContain("P");
    expect(CAT_PAW[0]).not.toContain("P");
    expect(pixelSvg(["P."], { P: "#000" }, 2)).toBe('<svg xmlns="http://www.w3.org/2000/svg" width="4" height="2" viewBox="0 0 2 1" shape-rendering="crispEdges"><rect x="0" y="0" width="1" height="1" style="fill:#000"/></svg>');
  });

  it("scope a species, and treat anything else as unknown", () => {
    expect(fxScope("cat")["data-fx-species"]).toBe("cat");
    expect(fxScope("dog")["data-fx-species"]).toBe("dog");
    expect(fxScope("")["data-fx-species"]).toBe("unknown");
    expect(fxScope(null)["data-fx-species"]).toBe("unknown");
    const stamp = (species: string | null) => (fxScope(species).style as Record<string, string>)["--fx-stamp"];
    expect(new Set([stamp("cat"), stamp("dog"), stamp(null)]).size).toBe(3);
  });
});
