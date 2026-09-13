// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  getStats: vi.fn(),
  getStatsAreas: vi.fn(),
  getStatsObservers: vi.fn(),
}));

import { getStats, getStatsAreas, getStatsObservers } from "../api";
import Stats from "./Stats";

afterEach(cleanup);

function mock({ areas = [], suppressed = 0, unattributed = 0, observers = [] } = {}) {
  vi.mocked(getStats).mockResolvedValue({
    kind: "bbmp_ward",
    totals: { observers: 11, sightings: 71, confirmed_individuals: 2 },
    months: [{ month: "2026-08", observers: 7, sightings: 27, confirmed_individuals: 2 }],
    areas_reported: areas.length,
    areas_suppressed: suppressed,
    unattributed_sightings: unattributed,
  });
  vi.mocked(getStatsAreas).mockResolvedValue({
    kind: "bbmp_ward",
    areas: areas as never,
    areas_suppressed: suppressed,
    unattributed_sightings: unattributed,
  });
  vi.mocked(getStatsObservers).mockResolvedValue({ observers: observers as never });
}

describe("Stats", () => {
  it("never prints one number and calls it the dog count", async () => {
    mock();
    render(<Stats onUnauthorized={() => {}} />);
    await waitFor(() => expect(screen.getByText("71")).toBeInTheDocument());
    // Sightings overcount, confirmed individuals undercount. The screen has to
    // say so, or a reader treats whichever number they saw as exact.
    expect(screen.getByText(/the true\s+number of dogs is somewhere between/i)).toBeInTheDocument();
  });

  it("explains an empty area table instead of implying there is no data", async () => {
    // The state prod is actually in: plenty of sightings, no reportable area.
    mock({ areas: [], suppressed: 8, unattributed: 55 });
    render(<Stats onUnauthorized={() => {}} />);

    await waitFor(() =>
      expect(screen.getByText(/No area is dense enough to show yet/i)).toBeInTheDocument(),
    );
    expect(screen.getByText("8")).toBeInTheDocument();
    expect(screen.getByText("55")).toBeInTheDocument();
    expect(
      screen.getByText(/logged somewhere this map doesn't\s+cover yet, not missing/i),
    ).toBeInTheDocument();
  });

  it("shows who is contributing, including people who logged nothing", async () => {
    mock({
      observers: [
        {
          id: "o1", display_name: "Priya", email: "priya@example.test",
          created_via: "email", trust_tier: null, created_at: "2026-08-01T00:00:00Z",
          sightings: 12, confirmed_individuals: 2, last_sighting_at: "2026-09-01T00:00:00Z",
        },
        {
          id: "o2", display_name: "Newcomer", email: null,
          created_via: "passcode", trust_tier: null, created_at: "2026-09-01T00:00:00Z",
          sightings: 0, confirmed_individuals: 0, last_sighting_at: null,
        },
      ],
    });
    render(<Stats onUnauthorized={() => {}} />);

    await waitFor(() => expect(screen.getByText("Priya")).toBeInTheDocument());
    // Someone who signed in and never captured is exactly what an operator
    // wants to see, so an empty row is a feature rather than noise.
    expect(screen.getByText("Newcomer")).toBeInTheDocument();
    expect(screen.getByText("—")).toBeInTheDocument();
  });
});
