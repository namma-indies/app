// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import CaptureReview from "./CaptureReview";
import { getCapture, type CaptureDetail, type CaptureInstance } from "../captureApi";

vi.mock("../haptics", () => ({ haptic: vi.fn() }));
vi.mock("../captureApi", () => ({ getCapture: vi.fn(), reviewCapture: vi.fn() }));
vi.mock("./VoiceNote", () => ({ default: () => <button type="button">🎙 add a voice note</button>, voiceNotesEnabled: () => true }));
beforeEach(() => { vi.useFakeTimers({ shouldAdvanceTime: true }); });
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.useRealTimers(); });

const instance = (id: string, track_id: string): CaptureInstance =>
  ({ id, photo_id: "frame1", track_id, sighting_id: null, species: "dog", thumb_url: `${id}.webp` });
const detail: CaptureDetail = {
  capture_id: "c", processing_state: "ready", captured_at: "2026-10-24T12:00:00Z", note: null, revision: 3,
  sighting_ids: ["s-a", "s-b"], groups: [{ instance_ids: ["a"] }, { instance_ids: ["b"] }],
  instances: [instance("a", "t1"), instance("b", "t2")],
  later_notes: [{ id: "n1", sighting_id: null, added_at: "2026-10-30T09:00:00Z", audio_url: "n1.m4a", state: "transcribing" }],
};

describe("Add voice note in the details sheet (#156)", () => {
  it("is offered only when the Journal says it can be", async () => {
    vi.mocked(getCapture).mockResolvedValue({ ...detail, later_notes: [] });
    const { unmount } = render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    await screen.findByRole("group", { name: "Dog 1" });
    expect(screen.queryByRole("button", { name: "🎙 add a voice note" })).not.toBeInTheDocument();
    unmount();
    render(<CaptureReview captureId="c" laterNotes={{ enabled: true, max: 5 }} onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    expect(await screen.findByRole("button", { name: "🎙 add a voice note" })).toBeInTheDocument();
  });

  it("fills in what a pending note says without reloading the sheet over unsaved edits", async () => {
    vi.mocked(getCapture).mockResolvedValueOnce(detail).mockResolvedValue({
      ...detail,
      later_notes: [{ ...detail.later_notes![0], state: "done" }],
      voice_transcripts: [{ source: "note", language: "en", text: "The brown one is Kaalu.", note_id: "n1", added_at: "2026-10-30T09:00:00Z", audio_url: "n1.m4a" }],
    });
    render(<CaptureReview captureId="c" laterNotes={{ enabled: true, max: 5 }} onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    expect(await screen.findByText("Listening to your note…")).toBeInTheDocument();
    const name = within(screen.getByRole("group", { name: "Dog 1" })).getByLabelText("Known name (optional)");
    await userEvent.type(name, "Kaju");
    await vi.advanceTimersByTimeAsync(3000);
    expect(await screen.findByText("The brown one is Kaalu.")).toBeInTheDocument();
    expect(screen.queryByText("Listening to your note…")).not.toBeInTheDocument();
    expect(within(screen.getByRole("group", { name: "Dog 1" })).getByLabelText("Known name (optional)")).toHaveValue("Kaju");
    await vi.advanceTimersByTimeAsync(60000);
    await waitFor(() => expect(getCapture).toHaveBeenCalledTimes(2));
  });
});
