// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { getDex, getMap, getMe, type LaterNote, type Sighting } from "../api";
import { getCaptures } from "../captureApi";
import Dex from "./Dex";
vi.mock("../api", async (original) => ({ ...await original<typeof import("../api")>(), getDex: vi.fn(), getMap: vi.fn(), getMe: vi.fn() }));
vi.mock("../captureApi", async (original) => ({ ...await original<typeof import("../captureApi")>(), getCaptures: vi.fn() }));
vi.mock("../components/DogMap", () => ({ default: () => <div>map</div> }));
vi.mock("../components/VoiceNote", () => ({ default: () => <button type="button">🎙 add a voice note</button>, voiceNotesEnabled: () => true }));

beforeEach(() => { vi.useFakeTimers({ shouldAdvanceTime: true }); });
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.useRealTimers(); });

const note = (id: string, state: LaterNote["state"], sighting_id = "a"): LaterNote =>
  ({ id, sighting_id, added_at: "2026-10-30T09:00:00Z", audio_url: `${id}.m4a`, state });
const sighting = (over: Partial<Sighting> = {}): Sighting => ({ id: "a", capture_id: "a", captured_at: "2026-09-15T12:00:00Z", lat: 12, lng: 77,
  geo_accuracy_m: 5, attrs: {}, processing_state: "legacy", photos: [{ url: "a.webp", thumb_url: "a_thumb.webp" }], ...over });

async function open(sightings: Sighting[], me: { later_voice_notes_enabled?: boolean; later_voice_notes_max?: number }) {
  vi.mocked(getDex).mockResolvedValue({ sightings });
  vi.mocked(getMe).mockResolvedValue({ is_moderator: false, ...me } as Awaited<ReturnType<typeof getMe>>);
  vi.mocked(getMap).mockResolvedValue({ sightings: [] });
  vi.mocked(getCaptures).mockResolvedValue({ sightings: [] });
  render(<Dex onUnauthorized={() => {}} />);
  await userEvent.click(await screen.findByRole("button", { name: "JOURNAL" }));
  await userEvent.click(await screen.findByAltText("animal sighting"));
}

describe("Add voice note in the Journal viewer (#156)", () => {
  it("is offered on your sighting when the server can transcribe it", async () => {
    await open([sighting()], { later_voice_notes_enabled: true, later_voice_notes_max: 5 });
    expect(await screen.findByRole("button", { name: "🎙 add a voice note" })).toBeInTheDocument();
  });

  it("is not offered while the server has no worker transcribing", async () => {
    await open([sighting()], { later_voice_notes_enabled: false, later_voice_notes_max: 5 });
    await waitFor(() => expect(getMe).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "🎙 add a voice note" })).not.toBeInTheDocument();
  });

  it("counts only this animal's notes against its limit", async () => {
    await open([sighting({ later_notes: [note("1", "done"), note("2", "done"), note("3", "done", null as unknown as string)] })],
      { later_voice_notes_enabled: true, later_voice_notes_max: 2 });
    expect(await screen.findByText("2 notes have been added here, as many as it can take.")).toBeInTheDocument();
  });

  it("checks back while a note is being worked on, and stops once it is done", async () => {
    vi.mocked(getDex)
      .mockResolvedValueOnce({ sightings: [sighting({ later_notes: [note("1", "transcribing")] })] })
      .mockResolvedValue({ sightings: [sighting({ later_notes: [note("1", "done")],
        voice_transcripts: [{ source: "note", language: "en", text: "She limps.", note_id: "1", added_at: "2026-10-30T09:00:00Z" }] })] });
    vi.mocked(getMe).mockResolvedValue({ is_moderator: false } as Awaited<ReturnType<typeof getMe>>);
    vi.mocked(getMap).mockResolvedValue({ sightings: [] });
    vi.mocked(getCaptures).mockResolvedValue({ sightings: [] });
    render(<Dex onUnauthorized={() => {}} />);
    await waitFor(() => expect(getDex).toHaveBeenCalledTimes(1));
    await vi.advanceTimersByTimeAsync(3000);
    await waitFor(() => expect(getDex).toHaveBeenCalledTimes(2));
    await vi.advanceTimersByTimeAsync(60000);
    expect(getDex).toHaveBeenCalledTimes(2);
  });
});
