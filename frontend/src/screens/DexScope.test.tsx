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

// MapLibre needs a real canvas; the toggle logic is what's under test here.
const dogMapProps = vi.fn();
vi.mock("../components/DogMap", () => ({
  default: (props: { sightings: unknown[] }) => {
    dogMapProps(props);
    return <div data-testid="dogmap">{props.sightings.length} pins</div>;
  },
}));

import Dex from "./Dex";

afterEach(cleanup);

function mine(id: string) {
  return {
    id,
    captured_at: "2026-07-19T10:00:00Z",
    lat: 12.97,
    lng: 77.59,
    geo_accuracy_m: 8,
    attrs: {},
    photos: [{ url: `http://x/${id}.webp`, thumb_url: `http://x/${id}_thumb.webp` }],
  };
}

function theirs(id: string, observer: string) {
  return {
    id,
    captured_at: "2026-07-18T10:00:00Z",
    lat: 12.98,
    lng: 77.6,
    geo_accuracy_m: 8,
    attrs: {},
    observer,
    mine: false,
    photos: [{ thumb_url: `http://x/${id}_thumb.webp` }],
  };
}

beforeEach(() => {
  getDex.mockReset();
  getMap.mockReset();
  dogMapProps.mockReset();
});

describe("map scope: MINE by default, EVERYONE on request", () => {
  it("starts on MINE and does not fetch the cohort map at all", async () => {
    getDex.mockResolvedValue({ sightings: [mine("a"), mine("b")] });
    render(<Dex onUnauthorized={() => {}} />);

    await waitFor(() => expect(screen.getByTestId("dogmap")).toBeInTheDocument());
    expect(screen.getByText("MINE")).toHaveClass("active");
    // The point of defaulting to MINE: the /dex data is already loaded for the
    // journal, so the common case costs no second request.
    expect(getMap).not.toHaveBeenCalled();
    expect(screen.getByText("2 pins")).toBeInTheDocument();
  });

  it("fetches the cohort map once, on the first flip to EVERYONE", async () => {
    getDex.mockResolvedValue({ sightings: [mine("a")] });
    getMap.mockResolvedValue({ sightings: [theirs("x", "Aswin"), theirs("y", "Akash")] });
    render(<Dex onUnauthorized={() => {}} />);
    await waitFor(() => screen.getByTestId("dogmap"));

    await userEvent.click(screen.getByText("EVERYONE"));

    await waitFor(() => expect(screen.getByText("2 pins")).toBeInTheDocument());
    expect(getMap).toHaveBeenCalledTimes(1);

    // Flipping back and forth must not refetch.
    await userEvent.click(screen.getByText("MINE"));
    await userEvent.click(screen.getByText("EVERYONE"));
    await waitFor(() => expect(screen.getByText("2 pins")).toBeInTheDocument());
    expect(getMap).toHaveBeenCalledTimes(1);
  });

  it("hands the cohort sightings to the map, not the viewer's own", async () => {
    getDex.mockResolvedValue({ sightings: [mine("a")] });
    getMap.mockResolvedValue({ sightings: [theirs("x", "Aswin")] });
    render(<Dex onUnauthorized={() => {}} />);
    await waitFor(() => screen.getByTestId("dogmap"));

    await userEvent.click(screen.getByText("EVERYONE"));

    await waitFor(() => {
      const calls = dogMapProps.mock.calls;
      const last = calls[calls.length - 1][0] as { sightings: { id: string }[] };
      expect(last.sightings.map((s) => s.id)).toEqual(["x"]);
    });
  });

  it("a new tester with nothing of their own can still reach EVERYONE", async () => {
    // The dead end this replaces: the empty state used to short-circuit the
    // whole view, so someone who had just joined saw "no sightings yet" and had
    // no way through to the shared map.
    getDex.mockResolvedValue({ sightings: [] });
    getMap.mockResolvedValue({ sightings: [theirs("x", "Aswin")] });
    render(<Dex onUnauthorized={() => {}} />);

    await waitFor(() => expect(screen.getByText(/NO SIGHTINGS YET/)).toBeInTheDocument());
    await userEvent.click(screen.getByText("or see everyone else's"));

    await waitFor(() => expect(screen.getByText("1 pins")).toBeInTheDocument());
  });

  it("falls back to your own map when the cohort fetch fails", async () => {
    // Their sightings are already loaded, so there's no reason to show nothing.
    getDex.mockResolvedValue({ sightings: [mine("a")] });
    getMap.mockRejectedValue(new Error("boom"));
    render(<Dex onUnauthorized={() => {}} />);
    await waitFor(() => screen.getByTestId("dogmap"));

    await userEvent.click(screen.getByText("EVERYONE"));

    await waitFor(() =>
      expect(screen.getByText(/Couldn't load the shared map/)).toBeInTheDocument(),
    );
    expect(screen.getByText("1 pins")).toBeInTheDocument();
  });

  it("signs you out when the cohort fetch says the session expired", async () => {
    const { UnauthorizedError } = await import("../api");
    const onUnauthorized = vi.fn();
    getDex.mockResolvedValue({ sightings: [mine("a")] });
    getMap.mockRejectedValue(new UnauthorizedError());
    render(<Dex onUnauthorized={onUnauthorized} />);
    await waitFor(() => screen.getByTestId("dogmap"));

    await userEvent.click(screen.getByText("EVERYONE"));

    await waitFor(() => expect(onUnauthorized).toHaveBeenCalled());
  });

  it("says so plainly when nobody has logged anything anywhere", async () => {
    getDex.mockResolvedValue({ sightings: [] });
    getMap.mockResolvedValue({ sightings: [] });
    render(<Dex onUnauthorized={() => {}} />);
    await waitFor(() => screen.getByText(/NO SIGHTINGS YET/));

    await userEvent.click(screen.getByText("EVERYONE"));

    await waitFor(() =>
      expect(screen.getByText("NO SIGHTINGS ANYWHERE YET")).toBeInTheDocument(),
    );
  });

  it("the journal stays yours, whatever the map is showing", async () => {
    // /map carries no full-resolution URLs, so the journal and its viewer read
    // from /dex regardless of scope.
    getDex.mockResolvedValue({ sightings: [mine("a")] });
    getMap.mockResolvedValue({ sightings: [theirs("x", "Aswin"), theirs("y", "A")] });
    render(<Dex onUnauthorized={() => {}} />);
    await waitFor(() => screen.getByTestId("dogmap"));
    await userEvent.click(screen.getByText("EVERYONE"));
    await waitFor(() => screen.getByText("2 pins"));

    await userEvent.click(screen.getByText("JOURNAL"));

    expect(screen.getByText(/YOUR GUIDE · 1 SIGHTING/)).toBeInTheDocument();
    // The scope toggle belongs to the map, not the journal.
    expect(screen.queryByText("EVERYONE")).not.toBeInTheDocument();
  });
});
