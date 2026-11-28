// @vitest-environment jsdom
// A dog is the same dog wherever they are mentioned: the Journal entry, the
// recognition card and their card under DOGS all carry one number, and each
// mention is a way through to the card.
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { getDex, getDog, getDogs, getMap, getMe, getProposals, getRecognised, type Dog, type Recognition, type Sighting } from "../api";
import { getCaptures } from "../captureApi";
import Dex from "./Dex";

vi.mock("../api", async (original) => ({
  ...(await original<typeof import("../api")>()),
  getDex: vi.fn(), getMap: vi.fn(), getMe: vi.fn(), getDogs: vi.fn(), getProposals: vi.fn(),
  getRecognised: vi.fn(), markRecognisedSeen: vi.fn(async () => {}), getDog: vi.fn(),
}));
vi.mock("../captureApi", async (original) => ({ ...(await original<typeof import("../captureApi")>()), getCaptures: vi.fn() }));
vi.mock("../components/DogMap", () => ({ default: () => <div>map</div> }));
vi.mock("../haptics", async (original) => ({ ...(await original<typeof import("../haptics")>()), haptic: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

function sighting(over: Partial<Sighting>): Sighting {
  return { id: "s1", captured_at: "2026-11-12T09:00:00Z", lat: 12.97, lng: 77.59, geo_accuracy_m: 8,
    attrs: {}, photos: [], individual: null, ...over } as Sighting;
}

function dog(over: Partial<Dog>): Dog {
  return { id: "d47", name: null, number: 47, company: [], first_seen: "2026-03-03", last_seen: "2026-11-12",
    sighting_count: 2, observer_count: 1, seen_by_me: true, observers: [], photos: [], lat: null, lng: null,
    precision: "none", cell_m: null, tags: [], looks_like: [], ...over };
}

const recognition: Recognition = {
  individual: { id: "d47", number: 47, name: null, species: "dog" },
  recognised_at: "2026-11-27T10:15:30.123456+00:00",
  yours: { id: "s1", captured_at: "2026-11-12T09:00:00+00:00", thumb_url: null },
  other: { captured_at: "2026-03-03T09:00:00+00:00", thumb_url: null, mine: true, observer: null },
  met_count: 2, sighting_count: 2, observers: [],
};

function setup(sightings: Sighting[], recognised: Recognition[] = []) {
  vi.mocked(getDex).mockResolvedValue({ sightings });
  vi.mocked(getMap).mockResolvedValue({ sightings: [] });
  vi.mocked(getMe).mockResolvedValue({ is_moderator: false } as Awaited<ReturnType<typeof getMe>>);
  vi.mocked(getCaptures).mockResolvedValue({ sightings: [] });
  vi.mocked(getProposals).mockResolvedValue({ proposals: [], propose_min: 0.4 });
  vi.mocked(getRecognised).mockResolvedValue({ recognised, more: 0 });
  // Two dogs, the one in question second, so "marked" cannot pass by being first.
  vi.mocked(getDogs).mockResolvedValue({ dogs: [dog({ id: "d3", number: 3 }), dog({})], propose_min: 0.4 });
  vi.mocked(getDog).mockImplementation(async (id) => ({ ...dog(id === "d3" ? { id, number: 3 } : {}), sightings: [] }));
}

/** The animal whose page is open, by its title. */
const opened = () => [...document.querySelectorAll(".dog-page-title")].map((n) => n.textContent);

describe("who a sighting turned out to be", () => {
  it("fills the Journal's empty slot, and leaves the others saying unidentified", async () => {
    setup([
      sighting({ id: "s1", individual: { id: "d47", number: 47, name: null } }),
      sighting({ id: "s2", captured_at: "2026-11-10T09:00:00Z" }),
    ]);
    render(<Dex onUnauthorized={() => {}} />);
    await userEvent.click(await screen.findByText("JOURNAL"));
    expect(await screen.findByRole("button", { name: /NO\. 047: see them under DOGS/ })).toBeInTheDocument();
    expect(screen.getAllByText("— UNIDENTIFIED —")).toHaveLength(1);
  });

  it("says where in words once the place has a name, and the coordinates until then", async () => {
    setup([
      sighting({ id: "s1", place: "Indiranagar, Bengaluru" }),
      sighting({ id: "s2", captured_at: "2026-11-10T09:00:00Z", place: null }),
    ]);
    render(<Dex onUnauthorized={() => {}} />);
    await userEvent.click(await screen.findByText("JOURNAL"));
    // On the entry's second line, under "spotted 12 Nov · …".
    expect(await screen.findByText(/Indiranagar, Bengaluru/)).toBeInTheDocument();
    expect(screen.getByText(/12\.970, 77\.590/)).toBeInTheDocument();
  });

  it("goes from the Journal entry to their page", async () => {
    setup([sighting({ individual: { id: "d47", number: 47, name: null } })]);
    render(<Dex onUnauthorized={() => {}} />);
    await userEvent.click(await screen.findByText("JOURNAL"));
    await userEvent.click(await screen.findByRole("button", { name: /NO\. 047/ }));
    await waitFor(() => expect(opened()).toEqual(["— NO. 047 —"]));
    expect(getDog).toHaveBeenLastCalledWith("d47");
    // Their page, not the photo viewer the rest of the entry opens.
    expect(document.querySelector(".viewer-overlay")).toBeNull();
  });

  it("tells you on the map and in the Journal, and takes you to them", async () => {
    setup([sighting({ individual: { id: "d47", number: 47, name: null } })], [recognition]);
    render(<Dex onUnauthorized={() => {}} />);
    expect(await screen.findByText("RECOGNISED")).toBeInTheDocument();
    await userEvent.click(screen.getByText("JOURNAL"));
    expect(screen.getByText("RECOGNISED")).toBeInTheDocument();

    await userEvent.click(screen.getByText("SEE THEM"));
    await waitFor(() => expect(opened()).toEqual(["— NO. 047 —"]));
    expect(screen.queryByText("RECOGNISED")).not.toBeInTheDocument();
  });

  it("opens DOGS on the list from the tab itself", async () => {
    setup([sighting({ individual: { id: "d47", number: 47, name: null } })]);
    render(<Dex onUnauthorized={() => {}} />);
    await userEvent.click(await screen.findByText("JOURNAL"));
    await userEvent.click(await screen.findByRole("button", { name: /NO\. 047/ }));
    await waitFor(() => expect(opened()).toHaveLength(1));
    await userEvent.click(screen.getByText("MAP"));
    await userEvent.click(screen.getByText("DOGS"));
    expect(await screen.findByText(/2 INDIES IDENTIFIED/)).toBeInTheDocument();
    expect(opened()).toEqual([]);
  });
});
