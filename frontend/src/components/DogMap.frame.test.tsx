// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import type { MappableSighting } from "../api";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Any = any;
const made = vi.hoisted(() => ({ maps: [] as Any[] }));
vi.mock("maplibre-gl", () => {
  class FakeMap {
    handlers: Record<string, (() => void)[]> = {};
    controls: Any[] = [];
    data: Any[] = [];
    fits: Any[][] = [];
    jumps: Any[] = [];
    source: Any = null;
    constructor(public opts: Any) { made.maps.push(this); }
    on(ev: string, cb: () => void) { (this.handlers[ev] ??= []).push(cb); }
    fire(ev: string) { for (const cb of this.handlers[ev] ?? []) cb(); }
    addControl(c: Any) { this.controls.push(c); }
    addSource() { this.source = { setData: (d: Any) => this.data.push(d) }; }
    addLayer() {}
    getSource() { return this.source; }
    isStyleLoaded() { return true; }
    isSourceLoaded() { return false; }
    querySourceFeatures() { return []; }
    getContainer() { return this.opts.container; }
    fitBounds(b: Any) { this.fits.push(b.pts); }
    jumpTo(o: Any) { this.jumps.push(o); }
    remove() {}
  }
  class NavigationControl { kind = "navigation"; }
  class GeolocateControl { kind = "geolocate"; constructor(public opts: Any) {} }
  class LngLatBounds { pts: Any[] = []; extend(p: Any) { this.pts.push(p); return this; } }
  return { default: { Map: FakeMap, NavigationControl, GeolocateControl, LngLatBounds } };
});

const fix = vi.hoisted(() => ({ resolve: (_: { lat: number; lng: number } | null) => {} }));
vi.mock("../capture/geolocate", () => ({
  locateQuietly: () => new Promise((resolve) => { fix.resolve = resolve; }),
}));

import DogMap from "./DogMap";

const INDIRANAGAR = { lat: 12.9784, lng: 77.6408 };
const NEXT_STREET = { lat: 12.9811, lng: 77.6408 };
const GOA = { lat: 15.2993, lng: 74.124 };
const BERKELEY = { lat: 37.8715, lng: -122.273 };

let n = 0;
function at(p: { lat: number; lng: number }, captured_at: string): MappableSighting {
  n += 1;
  return { id: `s${n}`, ...p, captured_at, attrs: {}, photos: [] };
}

/** Everything in one part of town, plus a holiday and a conference. */
const spread = () => [
  at(BERKELEY, "2026-08-01T08:00:00Z"),
  at(GOA, "2026-10-10T08:00:00Z"),
  at(NEXT_STREET, "2026-09-01T08:00:00Z"),
  at(INDIRANAGAR, "2026-11-20T08:00:00Z"),
];

function open(sightings: MappableSighting[]) {
  const view = render(<DogMap sightings={sightings} />);
  const map = made.maps[made.maps.length - 1];
  act(() => map.fire("load"));
  return { map, ...view };
}

beforeEach(() => { made.maps.length = 0; });
afterEach(cleanup);

describe("where the map opens", () => {
  it("opens on the neighbourhood of the latest sighting, not on every sighting at once", () => {
    const { map } = open(spread());
    expect(map.fits).toEqual([[
      [INDIRANAGAR.lng, INDIRANAGAR.lat],
      [NEXT_STREET.lng, NEXT_STREET.lat],
    ]]);
    // Everything is still on the map, a pinch away. Only the frame is local.
    expect(map.data[0].features).toHaveLength(4);
  });

  it("centres a lone sighting at neighbourhood zoom", () => {
    const { map } = open([at(GOA, "2026-10-10T08:00:00Z")]);
    expect(map.jumps).toEqual([{ center: [GOA.lng, GOA.lat], zoom: 15 }]);
  });

  it("does not re-frame when the sightings refresh", () => {
    const { map, rerender } = open(spread());
    rerender(<DogMap sightings={[...spread(), at(GOA, "2026-11-27T08:00:00Z")]} />);
    expect(map.data).toHaveLength(2);
    expect(map.fits).toHaveLength(1);
    expect(map.jumps).toHaveLength(0);
  });

  it("frames the first sighting that has a place when the first batch had none", () => {
    const nowhere: MappableSighting = { id: "x", lat: null, lng: null, captured_at: "2026-11-01T08:00:00Z", attrs: {}, photos: [] };
    const { map, rerender } = open([nowhere]);
    expect(map.jumps).toHaveLength(0);
    rerender(<DogMap sightings={[nowhere, at(GOA, "2026-11-02T08:00:00Z")]} />);
    expect(map.jumps).toEqual([{ center: [GOA.lng, GOA.lat], zoom: 15 }]);
  });

  it("offers a locate button that waits to be tapped", () => {
    const { map } = open(spread());
    expect(map.controls.map((c: Any) => c.kind)).toEqual(["navigation", "geolocate"]);
    expect(map.controls[1].opts.trackUserLocation).toBe(false);
  });
});

describe("a position the device gives up without being asked", () => {
  const here = { lat: 15.3001, lng: 74.124 };

  it("moves the opening frame to where you are, when there are sightings there", async () => {
    const { map } = open(spread());
    await act(async () => fix.resolve(here));
    expect(map.fits).toHaveLength(2);
    expect(map.fits[1]).toEqual([[here.lng, here.lat], [GOA.lng, GOA.lat]]);
  });

  it("stays put when nothing has been logged where you are", async () => {
    const { map } = open(spread());
    await act(async () => fix.resolve({ lat: 48.8566, lng: 2.3522 }));
    expect(map.fits).toHaveLength(1);
    expect(map.jumps).toHaveLength(0);
  });

  it("leaves the map alone once you have taken hold of it", async () => {
    const { map, container } = open(spread());
    fireEvent.pointerDown(container.querySelector(".map-wrap")!);
    await act(async () => fix.resolve(here));
    expect(map.fits).toHaveLength(1);
  });

  it("is used for the opening frame when it arrives before the map has loaded", async () => {
    render(<DogMap sightings={spread()} />);
    const map = made.maps[made.maps.length - 1];
    await act(async () => fix.resolve(here));
    act(() => map.fire("load"));
    expect(map.fits).toEqual([[[here.lng, here.lat], [GOA.lng, GOA.lat]]]);
  });
});
