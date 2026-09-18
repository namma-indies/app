// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { UPLOAD_COMPLETE_EVENT } from "../processing";
import userEvent from "@testing-library/user-event";
import { getDex, getMap, getMe } from "../api";
import { getCaptures } from "../captureApi";
import Dex from "./Dex";
vi.mock("../api", async (original) => ({ ...await original<typeof import("../api")>(), getDex: vi.fn(), getMap: vi.fn(), getMe: vi.fn() }));
vi.mock("../captureApi", async (original) => ({ ...await original<typeof import("../captureApi")>(), getCaptures: vi.fn() }));
vi.mock("../components/DogMap", () => ({ default: ({ sightings }: { sightings: unknown[] }) => <div>{sightings.length} map observations</div> }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("capture progress beside the animal feed", () => {
  it("refreshes animal entries when a capture publishes after the first upload refresh", async () => {
    const child = { id: "a", capture_id: "c", processing_state: "ready" as const, captured_at: "2026-09-15T12:00:00Z", lat: 12, lng: 77, geo_accuracy_m: 5, attrs: {}, photos: [] };
    const capture = { capture_id: "c", processing_state: "needs_review" as const, captured_at: child.captured_at, note: null, sighting_ids: [] };
    vi.mocked(getDex).mockResolvedValueOnce({ sightings: [] }).mockResolvedValueOnce({ sightings: [] }).mockResolvedValue({ sightings: [child] });
    vi.mocked(getMe).mockResolvedValue({ is_moderator: false } as Awaited<ReturnType<typeof getMe>>);
    vi.mocked(getMap).mockResolvedValue({ sightings: [] });
    vi.mocked(getCaptures).mockResolvedValueOnce({ sightings: [capture] }).mockResolvedValue({ sightings: [{ ...capture, processing_state: "ready", sighting_ids: ["a"] }] });
    render(<Dex onUnauthorized={() => {}} />);
    await screen.findByRole("button", { name: "Review animals privately" });
    act(() => window.dispatchEvent(new CustomEvent(UPLOAD_COMPLETE_EVENT)));
    expect(await screen.findByText("1 map observations")).toBeInTheDocument();
    expect(screen.getByText("1 animal sighting saved")).toBeInTheDocument();
    expect(getDex).toHaveBeenCalledTimes(3);
  });

  it("keeps private, failed and empty uploads visible without adding animals to the journal or map", async () => {
    vi.mocked(getDex).mockResolvedValue({ sightings: ["a", "b"].map((id) => ({ id, captured_at: "2026-09-15T12:00:00Z", lat: 12, lng: 77, geo_accuracy_m: 5, attrs: {}, photos: [{ url: `${id}.webp`, thumb_url: `${id}_thumb.webp` }] })) });
    vi.mocked(getMe).mockResolvedValue({ is_moderator: false } as Awaited<ReturnType<typeof getMe>>);
    vi.mocked(getMap).mockResolvedValue({ sightings: [] });
    vi.mocked(getCaptures).mockResolvedValue({ sightings: (["needs_review", "failed", "no_animal"] as const).map((processing_state) => ({ capture_id: processing_state, processing_state, captured_at: "2026-09-15T12:00:00Z", note: null, sighting_ids: [] })) });
    render(<Dex onUnauthorized={() => {}} />);
    expect(await screen.findByText("2 map observations")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Review animals privately" })).toBeInTheDocument();
    expect(screen.getByText("Processing failed · upload saved")).toBeInTheDocument();
    expect(screen.getByText("No animal detected · matching unavailable")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "JOURNAL" }));
    await waitFor(() => expect(screen.getByText("YOUR GUIDE · 2 SIGHTINGS")).toBeInTheDocument());
    expect(screen.getAllByRole("img")).toHaveLength(2);
  });
});
