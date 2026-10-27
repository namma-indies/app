// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import SuggestionFlow from "./components/SuggestionFlow";
import Transcript from "./components/Transcript";
import { ARRIVE_MS, playFill } from "./suggestionFlow";

const text = "She is a female. Her name is Kaalu.";
const highlights = [
  { id: "sex", quote: "She is a female", label: "Sex: female" },
  { id: "name", quote: "Her name is Kaalu", label: "Name: Kaalu" },
];
const rect = { left: 10, top: 10, right: 60, bottom: 30, width: 50, height: 20, x: 10, y: 10, toJSON() {} } as DOMRect;

function scene(ids: string[]) {
  return <SuggestionFlow ids={ids}>
    <Transcript transcripts={[{ source: "note", language: "en", text }]} highlights={highlights} />
    <button className="chip suggested" data-fx-chip="sex">female</button>
    <button className="chip suggested" data-fx-chip="name">Kaalu</button>
  </SuggestionFlow>;
}

let animate: ReturnType<typeof vi.fn>;
beforeEach(() => {
  animate = vi.fn(() => ({ finished: Promise.resolve() }));
  Element.prototype.animate = animate as never;
  Range.prototype.getBoundingClientRect = () => rect;
  Element.prototype.getBoundingClientRect = () => rect;
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); delete (Element.prototype as { animate?: unknown }).animate; });

describe("the glow and flow around suggestions", () => {
  it("glows the words, flies them to their chip, then lights the chip, in order", async () => {
    const { container } = render(scene(["sex", "name"]));
    const [first, second] = [...container.querySelectorAll<HTMLElement>("mark")];
    expect(first).toHaveAttribute("data-fx-play");
    expect(first.style.getPropertyValue("--fx-delay")).toBe("0ms");
    expect(second.style.getPropertyValue("--fx-delay")).toBe("160ms");
    // The chip lights after the words have had time to arrive.
    expect(screen.getByRole("button", { name: "female" }).style.getPropertyValue("--fx-delay")).toBe(`${ARRIVE_MS}ms`);
    expect(animate).toHaveBeenCalledTimes(4 + 4); // "She is a female" + "Her name is Kaalu"
    // The layer of flying words never takes a tap, hides from assistive tech, and cleans up after itself.
    const layer = document.querySelector(".fx-layer")!;
    expect(layer).toHaveAttribute("aria-hidden", "true");
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(document.querySelector(".fx-layer")).toBeNull();
  });

  it("plays once: answering one suggestion does not replay the rest, a new one plays alone", () => {
    const { rerender } = render(scene(["sex", "name"]));
    animate.mockClear();
    rerender(scene(["name"]));
    expect(animate).not.toHaveBeenCalled();
    rerender(scene(["name", "sex"]));
    expect(animate).not.toHaveBeenCalled();
  });

  it("holds back a chip that is below the fold until it is first seen, then lights it once", () => {
    let seen: (entries: { isIntersecting: boolean }[]) => void = () => {};
    const disconnect = vi.fn();
    vi.stubGlobal("IntersectionObserver", class { constructor(cb: typeof seen) { seen = cb; } observe() {} disconnect = disconnect; });
    Element.prototype.getBoundingClientRect = function () {
      return this.getAttribute("data-fx-chip") === "name" ? { ...rect, top: 5000, bottom: 5020 } as DOMRect : rect;
    };
    render(scene(["name"]));
    const chip = screen.getByRole("button", { name: "Kaalu" });
    expect(chip).not.toHaveAttribute("data-fx-play");
    act(() => seen([{ isIntersecting: true }]));
    expect(chip).toHaveAttribute("data-fx-play");
    expect(chip.style.getPropertyValue("--fx-delay")).toBe("0ms");
    expect(disconnect).toHaveBeenCalled();
  });

  it("does nothing under reduced motion: the marks and chips are simply there", () => {
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: query.includes("reduce"), media: query }));
    const { container } = render(scene(["sex", "name"]));
    expect(animate).not.toHaveBeenCalled();
    expect(container.querySelector("[data-fx-play]")).toBeNull();
    expect(container.querySelectorAll("mark")).toHaveLength(2);
  });

  it("flies a used suggestion to where its field is after the layout has moved, and lights it", () => {
    // Answering a suggestion removes its row, so the field below moves up.
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
    expect(animate).toHaveBeenCalledTimes(1);
    // The last keyframe lands on the field's new centre (120, 110), not the old one (120, 510: 490px down).
    const keyframes = animate.mock.calls[0][0] as { transform: string }[];
    expect(keyframes[keyframes.length - 1].transform).toContain("translate(85px,90px)");
  });
});
