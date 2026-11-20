// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { act, cleanup, render, screen } from "@testing-library/react";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Any = any;
const made = vi.hoisted(() => ({ maps: [] as Any[], markers: [] as Any[] }));
vi.mock("maplibre-gl", () => {
  class FakeMap {
    sources: Record<string, Any> = {};
    layers: Any[] = [];
    removed = false;
    bounds: Any;
    handlers: Record<string, () => void> = {};
    constructor(public opts: Any) { made.maps.push(this); }
    fitBounds(b: Any) { this.bounds = b; }
    on(ev: string, cb: () => void) { this.handlers[ev] = cb; }
    addSource(id: string, s: Any) { this.sources[id] = s; }
    addLayer(l: Any) { this.layers.push(l); }
    remove() { this.removed = true; }
  }
  class Marker {
    at: Any;
    constructor(public opts: Any) { made.markers.push(this); }
    setLngLat(c: Any) { this.at = c; return this; }
    addTo() { return this; }
  }
  class LngLatBounds { pts: Any[] = []; extend(p: Any) { this.pts.push(p); return this; } }
  return { default: { Map: FakeMap, Marker, LngLatBounds } };
});

import PairMap from "./PairMap";

let observe: (visible: boolean) => void = () => {};
beforeEach(() => {
  made.maps.length = 0;
  made.markers.length = 0;
  vi.stubGlobal("IntersectionObserver", class {
    constructor(cb: (e: { isIntersecting: boolean }[]) => void) { observe = (v) => cb([{ isIntersecting: v }]); }
    observe() {}
    disconnect() {}
  });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const points = [
  { label: "A", location: { lat: 12.9716, lng: 77.5946, precision: "exact" as const, cell_m: null } },
  { label: "B", location: { lat: 12.9761, lng: 77.61531, precision: "area" as const, cell_m: 1000 } },
];

describe("PairMap", () => {
  it("pins the exact side, draws the other as its cell, and frames both", () => {
    render(<PairMap points={points} label="pair" />);
    expect(made.maps).toHaveLength(0);
    act(() => observe(true));
    expect(made.maps).toHaveLength(1);
    const map = made.maps[0];
    expect(map.opts.interactive).toBe(false);
    // One pin, at the exact point only. The area side is never a pin.
    expect(made.markers.map((m) => m.at)).toEqual([[77.5946, 12.9716]]);
    act(() => map.handlers.load());
    const cells = map.sources["pair-cells"].data.features;
    expect(cells).toHaveLength(1);
    expect(cells[0].properties.label).toBe("B");
    // Fitted to the whole cell, not just its centre.
    expect(map.bounds.pts).toHaveLength(1 + 5);
  });

  it("only holds a map while the card is on screen", () => {
    render(<PairMap points={points} label="pair" />);
    act(() => observe(true));
    act(() => observe(false));
    expect(made.maps[0].removed).toBe(true);
    expect(screen.getByRole("img", { name: "pair" })).toBeInTheDocument();
  });

  it("does not rebuild the map when the card re-renders with the same points", () => {
    const { rerender } = render(<PairMap points={points} label="pair" />);
    act(() => observe(true));
    rerender(<PairMap points={points.map((p) => ({ ...p }))} label="pair" />);
    expect(made.maps).toHaveLength(1);
    expect(made.maps[0].removed).toBe(false);
  });

  it("renders nothing without a point", () => {
    const { container } = render(<PairMap points={[]} label="pair" />);
    expect(container).toBeEmptyDOMElement();
  });
});
