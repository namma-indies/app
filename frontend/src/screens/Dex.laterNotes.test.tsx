// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { getDex, getMap, getMe, redoTranscript, type LaterNote, type Sighting } from "../api";
import { getCaptures } from "../captureApi";
import Dex from "./Dex";
vi.mock("../api", async (original) => ({ ...await original<typeof import("../api")>(), getDex: vi.fn(), getMap: vi.fn(), getMe: vi.fn(), redoTranscript: vi.fn() }));
vi.mock("../captureApi", async (original) => ({ ...await original<typeof import("../captureApi")>(), getCaptures: vi.fn() }));
vi.mock("../components/DogMap", () => ({ default: () => <div>map</div> }));
vi.mock("../components/VoiceNote", () => ({ default: () => <button type="button">🎙 add a voice note</button>, voiceNotesEnabled: () => true, canRecordVoice: () => true }));

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

describe("an AI photo description in the Journal (#160)", () => {
  it("shows in the viewer, and becomes the photo's alt text only once used", async () => {
    await open([sighting({ photo_description: { id: "d", text: "A tan dog with a red collar.", status: "open" },
      suggestions: [{ id: "s", field: "condition", value: "healthy", quote: "A tan dog", description_id: "d" }] })], {});
    expect(screen.getByText("AI · SUGGESTED")).toBeInTheDocument();
    expect(screen.getByText("FROM THE PHOTO · AI")).toBeInTheDocument();
    expect(screen.getAllByRole("img").map((i) => i.getAttribute("alt"))).not.toContain("A tan dog with a red collar.");
    cleanup();
    vi.mocked(getDex).mockResolvedValue({ sightings: [sighting({ photo_description: { id: "d", text: "A tan dog with a red collar.", status: "accepted" } })] });
    render(<Dex onUnauthorized={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "JOURNAL" }));
    await userEvent.click(await screen.findByAltText("A tan dog with a red collar."));   // the card's photo
    expect(screen.getAllByAltText("A tan dog with a red collar.")).toHaveLength(2);       // and the viewer's
    expect(screen.getByText("AI-WRITTEN")).toBeInTheDocument();
  });
});


it("checks back while the photo is still being described, until the description arrives", async () => {
  vi.mocked(getDex)
    .mockResolvedValueOnce({ sightings: [sighting({ photo_description: { id: "d", text: null, status: "describing" } })] })
    .mockResolvedValue({ sightings: [sighting({ photo_description: { id: "d", text: "A tan dog with a red collar.", status: "open" } })] });
  vi.mocked(getMe).mockResolvedValue({ is_moderator: false } as Awaited<ReturnType<typeof getMe>>);
  vi.mocked(getMap).mockResolvedValue({ sightings: [] });
  vi.mocked(getCaptures).mockResolvedValue({ sightings: [] });
  render(<Dex onUnauthorized={() => {}} />);
  await userEvent.click(await screen.findByRole("button", { name: "JOURNAL" }));
  await userEvent.click(await screen.findByAltText("animal sighting"));
  expect(screen.queryByText("AI · SUGGESTED")).not.toBeInTheDocument();
  await vi.advanceTimersByTimeAsync(3000);
  expect(await screen.findByText("A tan dog with a red collar.")).toBeInTheDocument();   // no reload needed
  const calls = vi.mocked(getDex).mock.calls.length;
  await vi.advanceTimersByTimeAsync(60000);
  expect(vi.mocked(getDex).mock.calls.length).toBe(calls);                              // and then it stops
});

it("stops checking back once a redo has ended, words or not", async () => {
  vi.mocked(getDex)
    .mockResolvedValueOnce({ sightings: [sighting({ relistening: [{ source: "note", state: "listening" }] })] })
    .mockResolvedValue({ sightings: [sighting({ relistening: [{ source: "note", state: "no_speech" }] })] });
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


it("looks again after a redo whose answer was lost, so one that went through shows (#170 review)", async () => {
  vi.stubGlobal("confirm", vi.fn(() => true));
  vi.mocked(redoTranscript).mockRejectedValue(new TypeError("Failed to fetch"));   // committed, reply lost
  vi.mocked(getDex)
    .mockResolvedValueOnce({ sightings: [sighting({ voice_transcripts: [{ id: "t1", source: "note", language: "hi", text: "वह सो रही है", redo_language: "en" }] })] })
    .mockResolvedValue({ sightings: [sighting({ voice_transcripts: [], relistening: [{ source: "note", state: "listening" }] })] });
  vi.mocked(getMe).mockResolvedValue({ is_moderator: false } as Awaited<ReturnType<typeof getMe>>);
  vi.mocked(getMap).mockResolvedValue({ sightings: [] });
  vi.mocked(getCaptures).mockResolvedValue({ sightings: [] });
  render(<Dex onUnauthorized={() => {}} />);
  await userEvent.click(await screen.findByRole("button", { name: "JOURNAL" }));
  await userEvent.click(await screen.findByAltText("animal sighting"));
  await userEvent.click(screen.getByRole("button", { name: "Wrong language? Redo in English" }));
  expect(await screen.findByText("Listening again…")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Redo in/ })).not.toBeInTheDocument();
  vi.unstubAllGlobals();
});


it("keeps checking back after a redo's words arrive until its suggestions do (#170 review)", async () => {
  const words = { id: "t2", source: "note" as const, language: "en", text: "She is asleep." };
  vi.mocked(getDex)
    .mockResolvedValueOnce({ sightings: [sighting({ voice_transcripts: [{ ...words, reading: true }] })] })
    .mockResolvedValue({ sightings: [sighting({ voice_transcripts: [{ ...words, reading: false }],
      suggestions: [{ id: "s1", field: "sex", value: "female", quote: "She is asleep" }] })] });
  vi.mocked(getMe).mockResolvedValue({ is_moderator: false } as Awaited<ReturnType<typeof getMe>>);
  vi.mocked(getMap).mockResolvedValue({ sightings: [] });
  vi.mocked(getCaptures).mockResolvedValue({ sightings: [] });
  render(<Dex onUnauthorized={() => {}} />);
  await userEvent.click(await screen.findByRole("button", { name: "JOURNAL" }));
  await userEvent.click(await screen.findByAltText("animal sighting"));
  expect(screen.getByRole("status")).toHaveTextContent("Reading it for details…");
  await vi.advanceTimersByTimeAsync(3000);
  expect(await screen.findByText("FROM YOUR VOICE NOTE")).toBeInTheDocument();   // the chips came without a reload
  expect(screen.queryByText("Reading it for details…")).not.toBeInTheDocument();
});
