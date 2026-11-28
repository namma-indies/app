// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("../api", async (original) => ({ ...(await original<typeof import("../api")>()), getDog: vi.fn() }));

import { getDog, HttpError, type DogPage as Page, type DogPageSighting } from "../api";
import DogPage from "./DogPage";

afterEach(() => { cleanup(); vi.clearAllMocks(); });

function mine(day: string, over: Partial<DogPageSighting> = {}): DogPageSighting {
  return { id: `s-${day}`, mine: true, observer: null, captured_at: `${day}T12:15:00+05:30`, day,
    place: "Indiranagar, Bengaluru", thumb_url: `https://x.test/${day}.webp`, ...over };
}

function theirs(day: string, observer = "Priya"): DogPageSighting {
  return { id: null, mine: false, observer, captured_at: null, day, place: null, thumb_url: `https://x.test/${day}.webp` };
}

function page(over: Partial<Page> = {}): Page {
  return {
    id: "d3", name: null, number: 3, species: "dog", first_seen: "2026-03-03", last_seen: "2026-11-12",
    sighting_count: 3, observer_count: 2, seen_by_me: true, observers: ["Priya"],
    photos: ["https://x.test/2026-11-12.webp"], lat: 12.97, lng: 77.64, precision: "exact", cell_m: null,
    place: "Indiranagar, Bengaluru", tags: ["female"], looks_like: [],
    company: [{ id: "d1", number: 1, together: 2 }],
    sightings: [mine("2026-11-12"), mine("2026-09-14"), theirs("2026-03-03")],
    ...over,
  };
}

function show(p: Page, faces = new Map<string, string | undefined>()) {
  vi.mocked(getDog).mockResolvedValue(p);
  const handlers = { onBack: vi.fn(), onOpen: vi.fn() };
  render(<DogPage id={p.id} {...handlers} onUnauthorized={() => {}} faces={faces} />);
  return handlers;
}

describe("a dog's own page", () => {
  it("leads with their number, the latest photograph and the facts", async () => {
    show(page());
    expect(await screen.findByRole("heading", { name: "— NO. 003 —" })).toBeInTheDocument();
    expect(document.querySelector(".dog-lead")).toHaveAttribute("src", "https://x.test/2026-11-12.webp");
    expect(screen.getByText(/3 SIGHTINGS/)).toBeInTheDocument();
    expect(screen.getByText(/LAST SEEN IN INDIRANAGAR, BENGALURU/)).toBeInTheDocument();
    expect(screen.getByText("also logged by Priya")).toBeInTheDocument();
    expect(screen.getByText("♀ FEMALE")).toBeInTheDocument();
  });

  it("puts their first and latest photographs side by side", async () => {
    show(page());
    expect(await screen.findByText("THEN AND NOW")).toBeInTheDocument();
    const pair = [...document.querySelectorAll(".dog-section .recognised-side img")].map((i) => i.getAttribute("src"));
    expect(pair).toEqual(["https://x.test/2026-03-03.webp", "https://x.test/2026-11-12.webp"]);
    expect(screen.getByText("THE SAME DOG · 8 MONTHS APART")).toBeInTheDocument();
  });

  it("has no then-and-now with only one day of photographs", async () => {
    show(page({ sightings: [mine("2026-11-12"), mine("2026-11-12", { id: "s2" })] }));
    await screen.findByText("EVERY SIGHTING");
    expect(screen.queryByText("THEN AND NOW")).not.toBeInTheDocument();
  });

  it("lists your sightings with their moment and place, and others' with the day and who", async () => {
    show(page());
    await screen.findByText("EVERY SIGHTING");
    const captions = [...document.querySelectorAll(".dog-sighting figcaption")].map((c) => c.textContent);
    // The hour depends on the machine's timezone; the shape does not.
    expect(captions[0]).toMatch(/^12 Nov · \d{1,2}:\d{2} [ap]m · Indiranagar, Bengaluru$/);
    expect(captions[2]).toBe("3 Mar · Priya");
  });

  it("opens a companion's page", async () => {
    const h = show(page(), new Map([["d1", "https://x.test/d1.webp"]]));
    const companion = await screen.findByRole("button", { name: "Open NO. 001" });
    expect(companion.querySelector("img")).toHaveAttribute("src", "https://x.test/d1.webp");
    expect(screen.getByText("2 TIMES")).toBeInTheDocument();
    await userEvent.click(companion);
    expect(h.onOpen).toHaveBeenCalledWith("d1");
  });

  it("says when only the latest sightings are listed", async () => {
    show(page({ sighting_count: 80 }));
    expect(await screen.findByText("The latest 3 of 80.")).toBeInTheDocument();
  });

  it("goes back to the list", async () => {
    const h = show(page());
    await userEvent.click(await screen.findByText("← All dogs"));
    expect(h.onBack).toHaveBeenCalled();
  });

  it("says so when the animal is not theirs to see", async () => {
    vi.mocked(getDog).mockRejectedValue(new HttpError(404));
    render(<DogPage id="gone" onBack={() => {}} onOpen={() => {}} onUnauthorized={() => {}} />);
    expect(await screen.findByText(/THIS ANIMAL ISN'T IN YOUR GUIDE/)).toBeInTheDocument();
  });
});
