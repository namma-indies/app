// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import SuggestionFlow from "./components/SuggestionFlow";
import Transcript from "./components/Transcript";
import { ARRIVE_MS, FIRST_AT_MS, playFill, STEP_MS, USED_STEP_MS } from "./suggestionFlow";
import { BONE, CAT_PAW, DOG_PAW, FISH, fxScope, pixelSvg, SPARKLE, TWINKLE } from "./fxSprites";
import { haptic } from "./haptics";

vi.mock("./haptics", () => ({ haptic: vi.fn() }));

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
  vi.useFakeTimers();
  animate = vi.fn(() => ({ finished: Promise.resolve(), cancel() {} }));
  Element.prototype.animate = animate as never;
  Range.prototype.getBoundingClientRect = () => rect;
  Element.prototype.getBoundingClientRect = () => rect;
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.mocked(haptic).mockClear();
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

  it("taps as each comet lands, in step with its ring", () => {
    render(scene(["sex", "name"]));
    vi.advanceTimersByTime(FIRST_AT_MS + ARRIVE_MS - 1);
    expect(haptic).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(haptic).toHaveBeenCalledTimes(1);
    expect(haptic).toHaveBeenCalledWith("tick");
    vi.advanceTimersByTime(STEP_MS);
    expect(haptic).toHaveBeenCalledTimes(2);
  });

  it("does not tap for a chip that was answered and removed before its comet arrived", () => {
    render(scene(["sex", "name"]));
    vi.advanceTimersByTime(FIRST_AT_MS + ARRIVE_MS - 200);
    screen.getByRole("button", { name: "female" }).remove(); // answered; the scope stays mounted
    vi.advanceTimersByTime(5000);
    expect(haptic).toHaveBeenCalledTimes(1); // only the other chip's landing
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
    expect(haptic).not.toHaveBeenCalled(); // reopening a sighting must not buzz every time
  });

  it("plays open suggestions first, then used ones on a shorter beat", () => {
    render(scene(["sex"], ["name"]));
    expect(delayOf(screen.getByTestId("filled-name"))).toBe(`${FIRST_AT_MS + STEP_MS + ARRIVE_MS}ms`);
    vi.advanceTimersByTime(FIRST_AT_MS + STEP_MS + USED_STEP_MS + 3000);
    expect(haptic).toHaveBeenCalledTimes(1); // only for the open one
  });

  it("takes all its timing from one speed setting, --fx-pace", () => {
    document.documentElement.style.setProperty("--fx-pace", "2");
    render(scene(["sex", "name"]));
    expect(delayOf(screen.getByRole("button", { name: "Kaalu" }))).toBe(`${2 * (FIRST_AT_MS + STEP_MS + ARRIVE_MS)}ms`);
    vi.advanceTimersByTime(2 * (FIRST_AT_MS + ARRIVE_MS) - 1);
    expect(haptic).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(haptic).toHaveBeenCalledTimes(1);
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

  it("does nothing under reduced motion: the marks, chips and their paw stamps are simply there", () => {
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: query.includes("reduce"), media: query }));
    const { container } = render(scene(["sex", "name"]));
    vi.runAllTimers();
    expect(animate).not.toHaveBeenCalled();
    expect(haptic).not.toHaveBeenCalled(); // the tap only accompanies the flight, which is skipped
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
