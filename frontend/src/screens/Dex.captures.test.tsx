// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { UPLOAD_COMPLETE_EVENT } from "../processing";
import userEvent from "@testing-library/user-event";
import { getDex, getMap, getMe, type Sighting } from "../api";
import { getCaptures, type CaptureSummary } from "../captureApi";
import Dex from "./Dex";
vi.mock("../api", async (original) => ({ ...await original<typeof import("../api")>(), getDex: vi.fn(), getMap: vi.fn(), getMe: vi.fn() }));
vi.mock("../captureApi", async (original) => ({ ...await original<typeof import("../captureApi")>(), getCaptures: vi.fn() }));
vi.mock("../components/DogMap", () => ({ default: ({ sightings }: { sightings: unknown[] }) => <div>{sightings.length} map observations</div> }));
vi.mock("../components/CaptureReview", () => ({ default: ({ captureId }: { captureId: string }) => <div>review sheet for {captureId}</div> }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

const sighting = (id: string, over: Partial<Sighting> = {}): Sighting => ({ id, captured_at: "2026-09-15T12:00:00Z", lat: 12, lng: 77, geo_accuracy_m: 5, attrs: {}, photos: [{ url: `${id}.webp`, thumb_url: `${id}_thumb.webp` }], ...over });
const upload = (capture_id: string, processing_state: CaptureSummary["processing_state"], over: Partial<CaptureSummary> = {}): CaptureSummary => ({ capture_id, processing_state, captured_at: "2026-09-24T16:29:00Z", note: null, sighting_ids: [], ...over });

function load(sightings: Sighting[], captures: CaptureSummary[]) {
  vi.mocked(getDex).mockResolvedValue({ sightings });
  vi.mocked(getMe).mockResolvedValue({ is_moderator: false } as Awaited<ReturnType<typeof getMe>>);
  vi.mocked(getMap).mockResolvedValue({ sightings: [] });
  vi.mocked(getCaptures).mockResolvedValue({ sightings: captures });
  render(<Dex onUnauthorized={() => {}} />);
}

async function journal() {
  await userEvent.click(await screen.findByRole("button", { name: "JOURNAL" }));
  return screen.findByText(/YOUR GUIDE/);
}

describe("uploads in the Journal", () => {
  it("shows an upload that needs you as a highlighted Journal entry, and nothing on the map", async () => {
    load([sighting("a")], [upload("held", "needs_review", { thumb_url: "held_thumb.webp" })]);
    expect(await screen.findByText("1 map observations")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /separate/i })).not.toBeInTheDocument();
    await journal();
    const entry = screen.getByRole("article", { name: "Upload that needs you" });
    expect(entry).toHaveClass("needs-review");
    expect(within(entry).getByRole("img")).toHaveAttribute("src", "held_thumb.webp");
    await userEvent.click(within(entry).getByRole("button", { name: "Separate the animals" }));
    expect(screen.getByText("review sheet for held")).toBeInTheDocument();
  });

  it("shows what was heard and sends a published animal's suggestions to its sheet", async () => {
    load([sighting("a", {
      capture_id: "c", processing_state: "ready", details_editable: true,
      voice_transcripts: [{ source: "note", language: "en", text: "She is a white cat. People call her Billi." }],
      review_suggestions: [{ id: "n", field: "known_name", value: "Billi", quote: "People call her Billi" }],
    })], [upload("c", "ready", { sighting_ids: ["a"] })]);
    await journal();
    await userEvent.click(screen.getByAltText("dog sighting"));
    expect(screen.getByText("People call her Billi").tagName).toBe("MARK");
    await userEvent.click(screen.getByRole("button", { name: "Review 1 suggestion from your note" }));
    expect(screen.getByText("review sheet for c")).toBeInTheDocument();
  });

  it("adds no entry of its own for an upload that finished", async () => {
    load([sighting("a")], [upload("done", "ready", { sighting_ids: ["a"] }), upload("empty", "no_animal")]);
    await journal();
    expect(screen.queryByText(/animal sightings? saved/)).not.toBeInTheDocument();
    expect(screen.queryByRole("article", { name: /upload/i })).not.toBeInTheDocument();
  });

  it("shows uploads still processing, or that failed, as plain entries", async () => {
    load([], [upload("q", "queued"), upload("f", "failed")]);
    await journal();
    expect(screen.getByText("Uploaded · waiting to process")).toBeInTheDocument();
    expect(screen.getByText("Processing failed · upload saved")).toBeInTheDocument();
    for (const entry of screen.getAllByRole("article", { name: "Upload in progress" })) expect(entry).not.toHaveClass("needs-review");
  });

  it("drops the entry and shows the sighting once a held upload publishes", async () => {
    vi.mocked(getDex).mockResolvedValueOnce({ sightings: [] }).mockResolvedValueOnce({ sightings: [] }).mockResolvedValue({ sightings: [sighting("a", { capture_id: "c", processing_state: "ready" })] });
    vi.mocked(getMe).mockResolvedValue({ is_moderator: false } as Awaited<ReturnType<typeof getMe>>);
    vi.mocked(getMap).mockResolvedValue({ sightings: [] });
    vi.mocked(getCaptures).mockResolvedValueOnce({ sightings: [upload("c", "needs_review")] }).mockResolvedValue({ sightings: [upload("c", "ready", { sighting_ids: ["a"] })] });
    render(<Dex onUnauthorized={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "JOURNAL" }));
    await screen.findByRole("article", { name: "Upload that needs you" });
    act(() => window.dispatchEvent(new CustomEvent(UPLOAD_COMPLETE_EVENT)));
    expect(await screen.findByText("YOUR GUIDE · 1 SIGHTING")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("article", { name: "Upload that needs you" })).not.toBeInTheDocument());
  });

  it("offers Edit animal details only where there are details to edit", async () => {
    load([
      sighting("old", { capture_id: "old", processing_state: "ready", details_editable: false }),
      sighting("new", { capture_id: "cap", processing_state: "ready", details_editable: true }),
    ], []);
    await journal();
    const buttons = screen.getAllByRole("button", { name: "Edit animal details" });
    expect(buttons).toHaveLength(1);
    await userEvent.click(buttons[0]);
    expect(screen.getByText("review sheet for cap")).toBeInTheDocument();
  });
});
