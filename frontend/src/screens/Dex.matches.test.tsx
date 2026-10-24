// @vitest-environment jsdom
// Matches live inside DOGS, not in a tab of their own. The whole loop a tester
// walks: open DOGS, see how many pairs are waiting, answer one, come back to a
// list that already has the dog in it.
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { getDex, getDogs, getMap, getMe, getProposals, resolveProposal, type Dog, type Proposal } from "../api";
import { getCaptures } from "../captureApi";
import Dex from "./Dex";

vi.mock("../api", async (original) => ({
  ...(await original<typeof import("../api")>()),
  getDex: vi.fn(), getMap: vi.fn(), getMe: vi.fn(),
  getDogs: vi.fn(), getProposals: vi.fn(), resolveProposal: vi.fn(),
}));
vi.mock("../captureApi", async (original) => ({ ...(await original<typeof import("../captureApi")>()), getCaptures: vi.fn() }));
vi.mock("../components/DogMap", () => ({ default: () => <div>map</div> }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

const pair: Proposal = {
  id: "p1",
  score: 0.44,
  a: { sighting_id: "s1", date: "2026-10-01", thumb_url: "https://x.test/a_thumb.webp", mine: true },
  b: { sighting_id: "s2", date: "2026-10-09", thumb_url: "https://x.test/b_thumb.webp", mine: false },
};

const dog: Dog = {
  id: "d1", name: null, first_seen: "2026-10-01", last_seen: "2026-10-09",
  sighting_count: 2, observer_count: 2, seen_by_me: true, observers: ["Priya"],
  photos: [], lat: null, lng: null, precision: "none", cell_m: null, tags: [], looks_like: [],
};

function setup() {
  vi.mocked(getDex).mockResolvedValue({ sightings: [] });
  vi.mocked(getMap).mockResolvedValue({ sightings: [] });
  vi.mocked(getMe).mockResolvedValue({ is_moderator: false } as Awaited<ReturnType<typeof getMe>>);
  vi.mocked(getCaptures).mockResolvedValue({ sightings: [] });
}

describe("matches inside the Dogs tab", () => {
  it("has no separate MATCHES tab", async () => {
    setup();
    render(<Dex onUnauthorized={() => {}} />);
    await screen.findByText("DOGS");
    expect(screen.queryByText("MATCHES")).not.toBeInTheDocument();
  });

  it("answers a pair from DOGS and comes back to the dog it made", async () => {
    setup();
    vi.mocked(getDogs).mockResolvedValueOnce({ dogs: [], propose_min: 0.4 });
    vi.mocked(getProposals).mockResolvedValue({ proposals: [pair], propose_min: 0.4 });
    vi.mocked(resolveProposal).mockResolvedValue(undefined);
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<Dex onUnauthorized={() => {}} />);

    await userEvent.click(await screen.findByText("DOGS"));
    await userEvent.click(await screen.findByText("1 MATCH WAITING"));
    expect(await screen.findByText(/2026-10-01 · YOUR PHOTO/)).toBeInTheDocument();
    expect(screen.getByText("2026-10-09")).toBeInTheDocument();

    // After the verdict the dogs list is refetched and now has them in it.
    vi.mocked(getDogs).mockResolvedValue({ dogs: [dog], propose_min: 0.4 });
    vi.mocked(getProposals).mockResolvedValue({ proposals: [], propose_min: 0.4 });
    await userEvent.click(screen.getByText("SAME DOG"));
    await waitFor(() => expect(resolveProposal).toHaveBeenCalledWith("p1", "same"));
    expect(await screen.findByText(/Saved as one dog/)).toBeInTheDocument();

    await userEvent.click(screen.getByText("← Back to dogs"));
    expect(await screen.findByText(/1 INDIE IDENTIFIED/)).toBeInTheDocument();
    expect(screen.queryByText(/MATCH WAITING/)).not.toBeInTheDocument();
    confirmSpy.mockRestore();
  });
});
