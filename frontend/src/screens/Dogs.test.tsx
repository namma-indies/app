// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  getDogs: vi.fn(),
}));

import { getDogs, UnauthorizedError, type Dog, type DogsResponse } from "../api";
import Dogs from "./Dogs";

// See the note in Explore.test.tsx: no shared beforeEach resetting the mock.
afterEach(cleanup);

function dog(over: Partial<Dog> = {}): Dog {
  return {
    id: "d1",
    name: null,
    first_seen: "2026-07-01",
    last_seen: "2026-07-20",
    sighting_count: 3,
    observer_count: 1,
    seen_by_me: true,
    observers: [],
    photos: ["https://example.test/a_thumb.webp"],
    lat: 12.9716,
    lng: 77.5946,
    precision: "exact",
    cell_m: null,
    tags: ["female", "injured"],
    looks_like: [],
    ...over,
  };
}

function resp(dogs: Dog[], propose_min = 0.71): DogsResponse {
  return { dogs, propose_min };
}

describe("Dogs", () => {
  it("leads with the number of animals, not the number of sightings", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp([dog({ id: "a" }), dog({ id: "b" })]));
    render(<Dogs onUnauthorized={() => {}} />);
    await waitFor(() => expect(screen.getByText(/2 INDIES IDENTIFIED/)).toBeInTheDocument());
  });

  it("shows the last-seen coordinates", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp([dog()]));
    render(<Dogs onUnauthorized={() => {}} />);
    await waitFor(() =>
      expect(screen.getByText(/12\.9716, 77\.5946/)).toBeInTheDocument(),
    );
  });

  it("names who else logged it, as the map popup does", async () => {
    vi.mocked(getDogs).mockResolvedValue(
      resp([dog({ observer_count: 3, observers: ["Priya", "Aswin"] })]),
    );
    render(<Dogs onUnauthorized={() => {}} />);
    await waitFor(() =>
      expect(screen.getByText(/logged by Priya, Aswin/)).toBeInTheDocument());
  });

  it("does not name you to yourself", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp([dog({ observers: [] })]));
    render(<Dogs onUnauthorized={() => {}} />);
    await waitFor(() => expect(screen.getByText(/NO\. 001/)).toBeInTheDocument());
    expect(screen.queryByText(/logged by/)).not.toBeInTheDocument();
  });

  it("renders a display name as text -- observers type their own at /join", async () => {
    // The same threat the map popup escapes for. React escapes by
    // construction; this pins that the name never reaches innerHTML.
    vi.mocked(getDogs).mockResolvedValue(
      resp([dog({ observers: ["<img src=x onerror=alert(1)>"] })]),
    );
    render(<Dogs onUnauthorized={() => {}} />);
    await waitFor(() =>
      expect(screen.getByText(/<img src=x/)).toBeInTheDocument());
    expect(document.querySelector("img[onerror]")).toBeNull();
  });

  it("falls back to an identity number while naming does not exist", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp([dog()]));
    render(<Dogs onUnauthorized={() => {}} />);
    await waitFor(() => expect(screen.getByText(/NO\. 001/)).toBeInTheDocument());
  });

  it("uses the name once one exists", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp([dog({ name: "Kalu" })]));
    render(<Dogs onUnauthorized={() => {}} />);
    await waitFor(() => expect(screen.getByText("Kalu")).toBeInTheDocument());
  });

  it("explains an empty catalogue instead of looking broken", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp([]));
    render(<Dogs onUnauthorized={() => {}} />);
    await waitFor(() =>
      expect(screen.getByText(/CONFIRM A MATCH AND THE FIRST ONE APPEARS HERE/)).toBeInTheDocument(),
    );
  });

  it("escalates a 401", async () => {
    vi.mocked(getDogs).mockImplementation(async () => {
      throw new UnauthorizedError();
    });
    const onUnauthorized = vi.fn();
    render(<Dogs onUnauthorized={onUnauthorized} />);
    await waitFor(() => expect(onUnauthorized).toHaveBeenCalled());
  });
});

describe("Dogs look-alikes", () => {
  it("asks rather than asserts, because look-alikes outscore real matches", async () => {
    vi.mocked(getDogs).mockResolvedValue(
      resp([
        dog({ id: "a", looks_like: [{ id: "b", similarity: 0.62 }] }),
        dog({ id: "b", photos: ["https://example.test/b_thumb.webp"] }),
      ]),
    );
    render(<Dogs onUnauthorized={() => {}} />);
    await waitFor(() => expect(screen.getByText(/SAME DOG\? — NEEDS A HUMAN/)).toBeInTheDocument());
    // The score is shown so a reviewer can calibrate; it must never be
    // presented as a verdict.
    expect(screen.getByText("0.62")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/MATCH CONFIRMED|SAME DOG$/);
  });

  it("marks scores that clear the review threshold", async () => {
    vi.mocked(getDogs).mockResolvedValue(
      resp(
        [
          dog({ id: "a", looks_like: [{ id: "b", similarity: 0.80 }] }),
          dog({ id: "b" }),
        ],
        0.71,
      ),
    );
    render(<Dogs onUnauthorized={() => {}} />);
    await waitFor(() => expect(screen.getByText("0.80")).toBeInTheDocument());
    expect(screen.getByText("0.80")).toHaveClass("score-high");
  });

  it("leaves the block out entirely when there is nothing to review", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp([dog()]));
    render(<Dogs onUnauthorized={() => {}} />);
    await waitFor(() => expect(screen.getByText(/NO\. 001/)).toBeInTheDocument());
    expect(screen.queryByText(/NEEDS A HUMAN/)).not.toBeInTheDocument();
  });

  it("survives a look-alike that is not in the returned page", async () => {
    // MAX_DOGS truncates the list; a neighbour can legitimately be missing.
    vi.mocked(getDogs).mockResolvedValue(
      resp([dog({ id: "a", looks_like: [{ id: "missing", similarity: 0.5 }] })]),
    );
    render(<Dogs onUnauthorized={() => {}} />);
    await waitFor(() => expect(screen.getByText("0.50")).toBeInTheDocument());
  });
});

// --- precision ---------------------------------------------------------------
// A dog card gathers one animal, its history and its last known place onto a
// single screen, which is the shape someone would want if they meant it harm.
// Full precision is reserved for animals this viewer photographed; the rest are
// collapsed to a grid cell server-side, and the card has to say so.
describe("Dogs location precision", () => {
  it("shows a full coordinate for a dog you have photographed", async () => {
    vi.mocked(getDogs).mockResolvedValue(resp([dog({ precision: "exact", cell_m: null })]));
    render(<Dogs onUnauthorized={() => {}} />);
    expect(await screen.findByText(/LAST SEEN 12\.9716, 77\.5946/)).toBeInTheDocument();
  });

  it("says it is an area, and rounds off the digits, for someone else's dog", async () => {
    vi.mocked(getDogs).mockResolvedValue(
      resp([dog({ seen_by_me: false, precision: "area", cell_m: 1000, lat: 12.96712, lng: 77.59407 })]),
    );
    render(<Dogs onUnauthorized={() => {}} />);

    const line = await screen.findByText(/LAST SEEN IN A ~1 KM AREA/);
    expect(line).toBeInTheDocument();
    // Four decimals on a coordinate accurate to a kilometre invites the reader
    // to believe the digits.
    expect(line.textContent).toContain("12.97, 77.59");
    expect(line.textContent).not.toContain("12.9671");
  });

  it("reports the radius the server used rather than a hardcoded one", async () => {
    vi.mocked(getDogs).mockResolvedValue(
      resp([dog({ seen_by_me: false, precision: "area", cell_m: 2500 })]),
    );
    render(<Dogs onUnauthorized={() => {}} />);
    expect(await screen.findByText(/~2\.5 KM AREA/)).toBeInTheDocument();
  });

  it("still says nothing when there is no location at all", async () => {
    vi.mocked(getDogs).mockResolvedValue(
      resp([dog({ lat: null, lng: null, precision: "none", cell_m: null })]),
    );
    render(<Dogs onUnauthorized={() => {}} />);
    expect(await screen.findByText(/NO LOCATION RECORDED/)).toBeInTheDocument();
  });
});
