// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const getDex = vi.fn();
const getMap = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, getDex: () => getDex(), getMap: () => getMap() };
});

// MapLibre needs a real canvas; the journal rows are what's under test here.
vi.mock("../components/DogMap", () => ({
  default: () => <div data-testid="dogmap" />,
}));

import Dex from "./Dex";

afterEach(cleanup);

function sighting(id: string, off_map_reason: "reported" | "hidden" | "no_animal" | null) {
  return {
    id,
    captured_at: "2026-07-19T10:00:00Z",
    lat: 12.97,
    lng: 77.59,
    geo_accuracy_m: 8,
    attrs: {},
    on_map: off_map_reason === null,
    off_map_reason,
    photos: [{ url: `http://x/${id}.webp`, thumb_url: `http://x/${id}_thumb.webp` }],
  };
}

beforeEach(() => {
  getDex.mockReset();
  getMap.mockReset();
});

describe("the journal says why an off-map sighting is off the map", () => {
  it("says REPORTED when a person reported it, whatever the detector thinks", async () => {
    getDex.mockResolvedValue({ sightings: [sighting("a", "reported")] });
    render(<Dex onUnauthorized={() => {}} />);
    await waitFor(() => screen.getByTestId("dogmap"));

    await userEvent.click(screen.getByText("JOURNAL"));

    expect(screen.getByText("REPORTED · UNDER REVIEW")).toBeInTheDocument();
  });

  it("says HIDDEN BY A MODERATOR when a moderator rejected it", async () => {
    getDex.mockResolvedValue({ sightings: [sighting("a", "hidden")] });
    render(<Dex onUnauthorized={() => {}} />);
    await waitFor(() => screen.getByTestId("dogmap"));

    await userEvent.click(screen.getByText("JOURNAL"));

    expect(screen.getByText("HIDDEN BY A MODERATOR")).toBeInTheDocument();
  });

  it("says no animal was detected when that is the only reason", async () => {
    getDex.mockResolvedValue({ sightings: [sighting("a", "no_animal")] });
    render(<Dex onUnauthorized={() => {}} />);
    await waitFor(() => screen.getByTestId("dogmap"));

    await userEvent.click(screen.getByText("JOURNAL"));

    expect(screen.getByText("NOT ON THE SHARED MAP · NO ANIMAL DETECTED")).toBeInTheDocument();
  });

  it("renders nothing when the sighting is on the map", async () => {
    getDex.mockResolvedValue({ sightings: [sighting("a", null)] });
    render(<Dex onUnauthorized={() => {}} />);
    await waitFor(() => screen.getByTestId("dogmap"));

    await userEvent.click(screen.getByText("JOURNAL"));

    expect(screen.queryByText(/REPORTED/)).not.toBeInTheDocument();
    expect(screen.queryByText(/HIDDEN BY A MODERATOR/)).not.toBeInTheDocument();
    expect(screen.queryByText(/NO ANIMAL DETECTED/)).not.toBeInTheDocument();
  });
});
