// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import VoiceNote, { MAX_NOTE_SECONDS, voiceNotesEnabled } from "./VoiceNote";

const stopTrack = vi.fn();
const getUserMedia = vi.fn();

class FakeRecorder {
  static isTypeSupported = (t: string) => t === "audio/mp4";
  state = "inactive";
  mimeType: string;
  ondataavailable: ((e: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  constructor(_stream: unknown, options?: { mimeType?: string }) {
    this.mimeType = options?.mimeType ?? "";
  }
  start() {
    this.state = "recording";
  }
  stop() {
    this.state = "inactive";
    this.ondataavailable?.({ data: new Blob(["voice"], { type: this.mimeType }) });
    this.onstop?.();
  }
}

function Harness({ onNote }: { onNote: (b: Blob | null) => void }) {
  const [note, setNote] = useState<Blob | null>(null);
  return <VoiceNote value={note} onChange={(b) => { setNote(b); onNote(b); }} />;
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  (globalThis as any).MediaRecorder = FakeRecorder;
  getUserMedia.mockReset().mockResolvedValue({ getTracks: () => [{ stop: stopTrack }] });
  Object.defineProperty(navigator, "mediaDevices", { value: { getUserMedia }, configurable: true });
  URL.createObjectURL = vi.fn(() => "blob:note");
  URL.revokeObjectURL = vi.fn();
  stopTrack.mockReset();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("VoiceNote", () => {
  it("asks for the microphone only when record is tapped", async () => {
    render(<Harness onNote={() => {}} />);
    expect(getUserMedia).not.toHaveBeenCalled();
    await act(async () => fireEvent.click(screen.getByText(/add a voice note/)));
    expect(getUserMedia).toHaveBeenCalledWith({ audio: true });
  });

  it("records, then offers play, record again and delete", async () => {
    const onNote = vi.fn();
    render(<Harness onNote={onNote} />);
    await act(async () => fireEvent.click(screen.getByText(/add a voice note/)));
    await act(async () => fireEvent.click(screen.getByLabelText("Stop recording")));
    const note = onNote.mock.calls[0][0] as Blob;
    expect(note.type).toBe("audio/mp4");
    expect(stopTrack).toHaveBeenCalled();
    expect(document.querySelector("audio")).toHaveAttribute("src", "blob:note");
    expect(screen.getByText("record again")).toBeInTheDocument();
    await act(async () => fireEvent.click(screen.getByText("delete")));
    expect(onNote).toHaveBeenLastCalledWith(null);
    expect(screen.getByText(/add a voice note/)).toBeInTheDocument();
  });

  it(`stops itself at ${MAX_NOTE_SECONDS} seconds`, async () => {
    const onNote = vi.fn();
    render(<Harness onNote={onNote} />);
    await act(async () => fireEvent.click(screen.getByText(/add a voice note/)));
    await act(async () => { vi.advanceTimersByTime(MAX_NOTE_SECONDS * 1000 + 500); });
    expect(onNote).toHaveBeenCalledTimes(1);
    expect(screen.queryByLabelText("Stop recording")).not.toBeInTheDocument();
  });

  it("says so when the microphone is refused", async () => {
    getUserMedia.mockRejectedValue(new Error("NotAllowedError"));
    render(<Harness onNote={() => {}} />);
    await act(async () => fireEvent.click(screen.getByText(/add a voice note/)));
    expect(screen.getByText(/Allow it in Settings/)).toBeInTheDocument();
  });
});

describe("launch flag", () => {
  afterEach(() => vi.unstubAllEnvs());
  it("ships dark", () => {
    expect(voiceNotesEnabled()).toBe(false);
  });
  it("turns on only for an explicit true", () => {
    vi.stubEnv("VITE_VOICE_NOTES_ENABLED", "1");
    expect(voiceNotesEnabled()).toBe(false);
    vi.stubEnv("VITE_VOICE_NOTES_ENABLED", "true");
    expect(voiceNotesEnabled()).toBe(true);
  });
});
