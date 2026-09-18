// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render } from "@testing-library/react";

import CatSprite, { CAT_PIXELS } from "./CatSprite";
import DogSprite from "./DogSprite";

/**
 * The sprites are hand-plotted character grids. Both components take their
 * width from row 0 and then index every other row at that width, so a row that
 * is one character short reads `undefined` and silently drops its pixels —
 * a typo that renders as a hole in the animal rather than an error.
 */

afterEach(cleanup);

describe("the pixel grids are rectangular", () => {
  it("keeps every cat row the same width as the first", () => {
    const w = CAT_PIXELS[0].length;
    const ragged = CAT_PIXELS.map((row, y) => [y, row.length] as const).filter(([, n]) => n !== w);
    expect(ragged).toEqual([]);
  });

  it("uses only characters the palette knows how to paint", () => {
    // An unknown character is skipped, so a stray letter is another silent hole.
    const allowed = new Set([" ", "#", "B", "S", "o"]);
    const strays = new Set<string>();
    for (const row of CAT_PIXELS) {
      for (const ch of row) if (!allowed.has(ch)) strays.add(ch);
    }
    expect([...strays]).toEqual([]);
  });
});

describe("they render as crisp, decorative pixels", () => {
  it("draws the cat without anti-aliasing", () => {
    const { container } = render(<CatSprite />);
    const svg = container.querySelector("svg");
    expect(svg).toHaveAttribute("shape-rendering", "crispEdges");
  });

  it("hides both animals from assistive tech", () => {
    // They carry no information a screen reader needs; the status message does.
    const cat = render(<CatSprite />).container.querySelector("svg");
    const dog = render(<DogSprite />).container.querySelector("svg");
    expect(cat).toHaveAttribute("aria-hidden", "true");
    expect(dog).toHaveAttribute("aria-hidden", "true");
  });

  it("scales by whole pixels so the grid stays square", () => {
    const { container } = render(<CatSprite scale={4} />);
    const svg = container.querySelector("svg")!;
    expect(svg.getAttribute("width")).toBe(String(CAT_PIXELS[0].length * 4));
    expect(svg.getAttribute("height")).toBe(String(CAT_PIXELS.length * 4));
  });
});
