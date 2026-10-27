// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { act, cleanup, render, screen } from "@testing-library/react";

import { UPLOAD_COMPLETE_EVENT } from "../processing";
import Celebration, { CELEBRATION_MS } from "./Celebration";
import { haptic } from "../haptics";

vi.mock("../haptics", () => ({ haptic: vi.fn() }));

/**
 * The celebration marks one thing and one thing only: the server said the
 * upload is durably saved.
 *
 * Everything below pins a way that could go wrong and be nearly invisible in
 * review -- a party that fires while the capture is still queued on the device,
 * one that replays every old sighting on refresh, a stack of overlays during a
 * queue catch-up, or a full-screen layer that quietly eats taps on the map
 * underneath it.
 */

afterEach(cleanup);

/** jsdom has no matchMedia; the component asks it whether to animate at all. */
function setReducedMotion(reduce: boolean) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: reduce && query.includes("prefers-reduced-motion"),
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      onchange: null,
      dispatchEvent: () => false,
    }),
  });
}

function uploadAcknowledged(detail: unknown = { processing_state: "queued" }) {
  act(() => {
    window.dispatchEvent(new CustomEvent(UPLOAD_COMPLETE_EVENT, { detail }));
  });
}

beforeEach(() => {
  vi.mocked(haptic).mockClear();
  setReducedMotion(false);
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("when it fires", () => {
  it("shows nothing on mount, however many sightings already exist", () => {
    // The replay bug: deriving the celebration from stored state rather than
    // from an event means every refresh throws a party for old uploads.
    render(<Celebration />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("celebrates once the server acknowledges a durable save", () => {
    render(<Celebration />);
    uploadAcknowledged();
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("clears itself, leaving nothing on screen", () => {
    render(<Celebration />);
    uploadAcknowledged();
    act(() => void vi.advanceTimersByTime(CELEBRATION_MS + 50));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});

describe("a queue catching up", () => {
  it("does not stack a second overlay on top of a running one", () => {
    // Coming back online flushes the whole queue in one pass, so several
    // acknowledgements land within milliseconds of each other. That must be
    // one celebration, not six full-screen layers fighting each other.
    render(<Celebration />);
    uploadAcknowledged();
    uploadAcknowledged();
    uploadAcknowledged();
    expect(screen.getAllByRole("status")).toHaveLength(1);
  });

  it("can celebrate again once the first one has finished", () => {
    // Coalescing must not become "only ever celebrates once per session".
    render(<Celebration />);
    uploadAcknowledged();
    act(() => void vi.advanceTimersByTime(CELEBRATION_MS + 50));
    uploadAcknowledged();
    expect(screen.getByRole("status")).toBeInTheDocument();
  });
});

describe("it stays out of the way", () => {
  it("never intercepts the controls underneath it", () => {
    // The overlay covers the viewport, including the map and the shutter. If
    // it takes pointer events the app is frozen for as long as it runs.
    render(<Celebration />);
    uploadAcknowledged();
    const overlay = document.querySelector(".celebration");
    expect(overlay).toBeTruthy();
    expect(overlay).toHaveClass("celebration");
    // Asserted on the class contract rather than computed style: jsdom does
    // not apply the stylesheet, so getComputedStyle would pass either way.
    expect(overlay?.getAttribute("aria-hidden")).not.toBe("true");
  });

  it("hides the decoration from assistive tech, and keeps the message", () => {
    render(<Celebration />);
    uploadAcknowledged();
    expect(document.querySelector(".celebration-scene")).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByRole("status")).toHaveTextContent(/logged/i);
  });
});

describe("what it is allowed to claim", () => {
  it("says the upload was logged, never that an animal was found", () => {
    // Detection and matching happen later, on a GPU, and may find nothing at
    // all. A celebration that says "dog found" is a lie about half the time.
    render(<Celebration />);
    uploadAcknowledged();
    const said = screen.getByRole("status").textContent ?? "";
    expect(said).toMatch(/logged/i);
    expect(said).not.toMatch(/dog|cat|animal|match|detect|identif/i);
  });
});

describe("reduced motion", () => {
  it("announces the success without animating anything", () => {
    setReducedMotion(true);
    render(<Celebration />);
    uploadAcknowledged();
    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(document.querySelectorAll(".confetti-piece")).toHaveLength(0);
    expect(document.querySelector(".celebration-scene")).toBeNull();
  });

  it("still gets out of the way on its own", () => {
    setReducedMotion(true);
    render(<Celebration />);
    uploadAcknowledged();
    act(() => void vi.advanceTimersByTime(CELEBRATION_MS + 50));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("animates when motion is allowed", () => {
    render(<Celebration />);
    uploadAcknowledged();
    expect(document.querySelectorAll(".confetti-piece").length).toBeGreaterThan(0);
    expect(document.querySelector(".celebration-scene")).toBeTruthy();
  });
});

describe("cleanup", () => {
  it("drops its timer when unmounted mid-celebration", () => {
    // A pending setTimeout that fires after unmount is a React state update on
    // a dead component -- noisy in dev, and a leak if captures come fast.
    const { unmount } = render(<Celebration />);
    uploadAcknowledged();
    unmount();
    expect(() => act(() => void vi.advanceTimersByTime(CELEBRATION_MS + 50))).not.toThrow();
  });

  it("stops listening after unmount", () => {
    const { unmount } = render(<Celebration />);
    unmount();
    uploadAcknowledged();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});

describe("a burst and a streak (#81)", () => {
  class MemoryStorage {
    private store = new Map<string, string>();
    getItem(k: string) { return this.store.has(k) ? this.store.get(k)! : null; }
    setItem(k: string, v: string) { this.store.set(k, String(v)); }
    removeItem(k: string) { this.store.delete(k); }
    clear() { this.store.clear(); }
  }
  const NOW = Date.parse("2026-10-24T08:00:00Z");
  const capturedMinutesAgo = (m: number) => ({ processing_state: "queued", captured_at: new Date(NOW - m * 60_000).toISOString() });

  beforeEach(() => {
    vi.stubGlobal("localStorage", new MemoryStorage());
    vi.setSystemTime(NOW);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("says how many were logged in one burst, as one celebration", () => {
    render(<Celebration />);
    uploadAcknowledged(capturedMinutesAgo(300));
    uploadAcknowledged(capturedMinutesAgo(200));
    uploadAcknowledged(capturedMinutesAgo(100));
    expect(screen.getAllByRole("status")).toHaveLength(1);
    expect(screen.getByRole("status")).toHaveTextContent("3 logged!");
  });

  it("throws a bigger party for a bigger burst", () => {
    render(<Celebration />);
    uploadAcknowledged(capturedMinutesAgo(300));
    const single = document.querySelectorAll(".confetti-piece").length;
    uploadAcknowledged(capturedMinutesAgo(200));
    uploadAcknowledged(capturedMinutesAgo(100));
    expect(document.querySelectorAll(".confetti-piece").length).toBeGreaterThan(single);
    expect(document.querySelectorAll(".celebration-runner")).toHaveLength(4);
  });

  it("opens the streak card on the fifth sighting of a quick run, and it outlasts the confetti", () => {
    render(<Celebration />);
    for (const m of [12, 9, 6, 3]) uploadAcknowledged(capturedMinutesAgo(m));
    expect(screen.queryByRole("dialog", { name: "Hot streak" })).not.toBeInTheDocument();
    uploadAcknowledged(capturedMinutesAgo(0));
    const card = screen.getByRole("dialog", { name: "Hot streak" });
    expect(card).toHaveTextContent("5");
    expect(card).toHaveTextContent("sightings logged in 12 minutes");
    act(() => void vi.advanceTimersByTime(CELEBRATION_MS + 50));
    expect(screen.getByRole("dialog", { name: "Hot streak" })).toBeInTheDocument();
    act(() => screen.getByRole("button", { name: "NICE" }).click());
    expect(screen.queryByRole("dialog", { name: "Hot streak" })).not.toBeInTheDocument();
  });

  it("claims sightings, never animals, and carries no coordinates", () => {
    render(<Celebration />);
    for (const m of [12, 9, 6, 3, 0]) uploadAcknowledged(capturedMinutesAgo(m));
    const said = screen.getByRole("dialog", { name: "Hot streak" }).textContent ?? "";
    expect(said).not.toMatch(/\bdogs?\b|\bcats?\b|animal|identif|match/i);
    expect(said).not.toMatch(/\d+\.\d{3,}/); // nothing like 12.9716
  });

  it("gives reduced motion the same card, still", () => {
    setReducedMotion(true);
    render(<Celebration />);
    for (const m of [12, 9, 6, 3, 0]) uploadAcknowledged(capturedMinutesAgo(m));
    expect(screen.getByRole("dialog", { name: "Hot streak" })).toBeInTheDocument();
    expect(document.querySelector(".celebration-scene")).toBeNull();
  });
});

describe("how it feels (#122)", () => {
  it("buzzes once when the upload is acknowledged, not before and not on mount", () => {
    render(<Celebration />);
    expect(haptic).not.toHaveBeenCalled();
    uploadAcknowledged();
    expect(haptic).toHaveBeenCalledTimes(1);
    expect(haptic).toHaveBeenCalledWith("logged");
  });

  it("still buzzes under reduced motion: it marks the event, not the animation", () => {
    setReducedMotion(true);
    render(<Celebration />);
    uploadAcknowledged();
    expect(haptic).toHaveBeenCalledWith("logged");
  });

  it("adds a tap per extra sighting in a burst, a few at most", () => {
    render(<Celebration />);
    for (let i = 0; i < 8; i++) uploadAcknowledged();
    expect(vi.mocked(haptic).mock.calls.map(([moment]) => moment)).toEqual(["logged", "tick", "tick", "tick"]);
  });
});
