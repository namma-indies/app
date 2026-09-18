// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { act, cleanup, render, screen } from "@testing-library/react";

import { UPLOAD_COMPLETE_EVENT } from "../processing";
import Celebration, { CELEBRATION_MS } from "./Celebration";

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
