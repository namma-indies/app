// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// Node's own built-in `localStorage` global (active under this Node version,
// regardless of jsdom) ships without a backing file and throws on every call
// -- unrelated to anything under test here, so it's swapped for a plain
// in-memory Storage rather than fighting the runtime for the real thing.
class MemoryStorage implements Storage {
  private store = new Map<string, string>();
  get length() {
    return this.store.size;
  }
  clear() {
    this.store.clear();
  }
  getItem(key: string) {
    return this.store.has(key) ? this.store.get(key)! : null;
  }
  key(index: number) {
    return [...this.store.keys()][index] ?? null;
  }
  removeItem(key: string) {
    this.store.delete(key);
  }
  setItem(key: string, value: string) {
    this.store.set(key, String(value));
  }
}

// Real MapLibre needs a canvas/WebGL context that jsdom doesn't provide, so the
// map itself is faked here -- the behaviour under test is what LocationPicker
// does with a map (controls added, center/zoom chosen, zoom read on confirm),
// not MapLibre's own rendering.
vi.mock("maplibre-gl", () => {
  class FakeMap {
    static instances: FakeMap[] = [];
    center: { lng: number; lat: number };
    zoom: number;
    handlers: Record<string, Array<() => void>> = {};
    controls: Array<{ control: unknown; position?: string }> = [];
    constructor(opts: { center: [number, number]; zoom: number }) {
      this.center = { lng: opts.center[0], lat: opts.center[1] };
      this.zoom = opts.zoom;
      FakeMap.instances.push(this);
    }
    on(event: string, cb: () => void) {
      (this.handlers[event] ??= []).push(cb);
    }
    addControl(control: unknown, position?: string) {
      this.controls.push({ control, position });
    }
    remove() {}
    getCenter() {
      return this.center;
    }
    getZoom() {
      return this.zoom;
    }
    setZoomForTest(z: number) {
      this.zoom = z;
    }
    panForTest(lng: number, lat: number) {
      this.center = { lng, lat };
      (this.handlers.move ?? []).forEach((cb) => cb());
    }
  }
  class FakeGeolocateControl {
    opts: unknown;
    constructor(opts: unknown) {
      this.opts = opts;
    }
  }
  return {
    default: { Map: FakeMap, GeolocateControl: FakeGeolocateControl },
  };
});

import maplibregl from "maplibre-gl";
import LocationPicker from "./LocationPicker";

type FakeMapT = {
  center: { lng: number; lat: number };
  zoom: number;
  controls: Array<{ control: unknown; position?: string }>;
  setZoomForTest: (z: number) => void;
  panForTest: (lng: number, lat: number) => void;
};
type FakeMapClass = typeof maplibregl.Map & { instances: FakeMapT[] };
const FakeMap = maplibregl.Map as unknown as FakeMapClass;

beforeEach(() => {
  vi.stubGlobal("localStorage", new MemoryStorage());
});

afterEach(() => {
  cleanup();
  FakeMap.instances.length = 0;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function openPickMap() {
  await userEvent.click(screen.getByText("PICK ON A MAP"));
  return FakeMap.instances[FakeMap.instances.length - 1];
}

describe("the pick map", () => {
  it("adds a GeolocateControl so a person can centre the map on themselves", async () => {
    render(<LocationPicker initial={null} onPick={() => {}} onClose={() => {}} />);
    const map = await openPickMap();

    expect(
      map.controls.some(
        (c) => c.control instanceof maplibregl.GeolocateControl && c.position === "top-right",
      ),
    ).toBe(true);
  });

  it("centres on the last pinned place when reopened with nothing in progress", async () => {
    localStorage.setItem(
      "indiedex:lastPinnedPlace",
      JSON.stringify({ lat: 10.2381, lng: 77.4892 }),
    );
    render(<LocationPicker initial={null} onPick={() => {}} onClose={() => {}} />);
    const map = await openPickMap();

    expect(map.center).toEqual({ lng: 77.4892, lat: 10.2381 });
    expect(map.zoom).toBe(16);
  });

  it("falls back to the Bangalore default when nothing has ever been pinned", async () => {
    render(<LocationPicker initial={null} onPick={() => {}} onClose={() => {}} />);
    const map = await openPickMap();

    expect(map.center).toEqual({ lng: 77.5946, lat: 12.9716 });
    expect(map.zoom).toBe(11);
  });

  it("refuses a pin dropped below zoom 14", async () => {
    const onPick = vi.fn();
    render(<LocationPicker initial={null} onPick={onPick} onClose={() => {}} />);
    const map = await openPickMap();
    map.setZoomForTest(12);

    await userEvent.click(screen.getByText("USE THIS SPOT"));

    expect(onPick).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(/zoom in closer/i);
    expect(localStorage.getItem("indiedex:lastPinnedPlace")).toBeNull();
  });

  it("accepts and remembers a pin dropped at or above zoom 14", async () => {
    const onPick = vi.fn();
    render(<LocationPicker initial={null} onPick={onPick} onClose={() => {}} />);
    const map = await openPickMap();
    map.setZoomForTest(14);
    map.panForTest(77.1, 11.1);

    await userEvent.click(screen.getByText("USE THIS SPOT"));

    expect(onPick).toHaveBeenCalledWith({ lat: 11.1, lng: 77.1, source: "pin" });
    expect(JSON.parse(localStorage.getItem("indiedex:lastPinnedPlace")!)).toEqual({
      lat: 11.1,
      lng: 77.1,
    });
  });
});
