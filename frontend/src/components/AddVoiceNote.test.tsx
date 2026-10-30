// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import AddVoiceNote, { laterNotesOffered } from "./AddVoiceNote";

// The recorder has its own tests (VoiceNote.test.tsx); here it only hands over a recording.
let buildHasVoiceNotes = true;
vi.mock("./VoiceNote", () => ({
  default: ({ value, onChange }: { value: Blob | null; onChange: (b: Blob | null) => void }) => value
    ? <button type="button" onClick={() => onChange(null)}>delete</button>
    : <button type="button" onClick={() => onChange(new Blob(["voice"], { type: "audio/mp4" }))}>🎙 add a voice note</button>,
  voiceNotesEnabled: () => buildHasVoiceNotes,
  canRecordVoice: () => true,
}));
// The language question has its own tests (NoteLanguagePicker.test.tsx); here it is answered.
vi.mock("./NoteLanguagePicker", async (original) => ({
  ...await original<typeof import("./NoteLanguagePicker")>(),
  useNoteLanguage: () => ({ usual: "en", keep: vi.fn() }),
}));

const fetchMock = vi.fn();
beforeEach(() => { buildHasVoiceNotes = true; fetchMock.mockReset(); vi.stubGlobal("fetch", fetchMock); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const reply = (status: number, body: unknown = {}) => new Response(JSON.stringify(body), { status });

function renderIt(over: Partial<Parameters<typeof AddVoiceNote>[0]> = {}) {
  const onAdded = vi.fn();
  const onUnauthorized = vi.fn();
  render(<AddVoiceNote target={{ sightingId: "s1" }} added={0} max={5} onAdded={onAdded} onUnauthorized={onUnauthorized} {...over} />);
  return { onAdded, onUnauthorized };
}

describe("adding a voice note after upload (#156)", () => {
  it("records with the capture screen's recorder, then saves the note to that sighting", async () => {
    fetchMock.mockResolvedValue(reply(202, { note_id: "n1", state: "transcribing" }));
    const { onAdded } = renderIt();
    expect(screen.queryByRole("button", { name: "Save note" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "🎙 add a voice note" }));
    await userEvent.click(screen.getByRole("button", { name: "Save note" }));
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toMatch(/\/sighting\/s1\/voice-note$/);
    expect(init.method).toBe("POST");
    const audio = (init.body as FormData).get("audio") as File;
    expect(audio.name).toBe("note.m4a");
    expect(onAdded).toHaveBeenCalledOnce();
    expect(screen.queryByRole("button", { name: "Save note" })).not.toBeInTheDocument();   // cleared for the next one
  });

  it("saves to the whole capture from its details sheet", async () => {
    fetchMock.mockResolvedValue(reply(202, { note_id: "n1" }));
    renderIt({ target: { captureId: "c1" } });
    await userEvent.click(screen.getByRole("button", { name: "🎙 add a voice note" }));
    await userEvent.click(screen.getByRole("button", { name: "Save note" }));
    expect(fetchMock.mock.calls[0][0]).toMatch(/\/capture\/c1\/voice-note$/);
  });

  it.each([
    [409, "This already has as many added notes as it can take."],
    [429, "You’ve added a lot of notes in the last hour. Try again later."],
    [503, "Notes can’t be added right now. Try again later."],
    [500, "Couldn’t save your note. Try again."],
  ])("says why a save failed (%i) and keeps the recording to try again", async (status, message) => {
    fetchMock.mockResolvedValue(reply(status));
    const { onAdded } = renderIt();
    await userEvent.click(screen.getByRole("button", { name: "🎙 add a voice note" }));
    await userEvent.click(screen.getByRole("button", { name: "Save note" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.getByRole("button", { name: "Save note" })).toBeEnabled();
    expect(onAdded).not.toHaveBeenCalled();
  });

  it("sends a signed-out person to sign in", async () => {
    fetchMock.mockResolvedValue(reply(401));
    const { onUnauthorized } = renderIt();
    await userEvent.click(screen.getByRole("button", { name: "🎙 add a voice note" }));
    await userEvent.click(screen.getByRole("button", { name: "Save note" }));
    await screen.findByRole("alert");
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });

  it("offers no recorder once the sighting has as many notes as it can take", () => {
    renderIt({ added: 5, max: 5 });
    expect(screen.queryByRole("button", { name: "🎙 add a voice note" })).not.toBeInTheDocument();
    expect(screen.getByText("5 notes have been added here, as many as it can take.")).toBeInTheDocument();
  });

  it("is offered only when the build has voice notes and the server can transcribe them", () => {
    expect(laterNotesOffered({ enabled: true, max: 5 })).toBe(true);
    expect(laterNotesOffered({ enabled: false, max: 5 })).toBe(false);
    expect(laterNotesOffered({ enabled: true, max: 0 })).toBe(false);
    expect(laterNotesOffered(null)).toBe(false);
    buildHasVoiceNotes = false;
    expect(laterNotesOffered({ enabled: true, max: 5 })).toBe(false);
  });

  it("sends a retry of the same recording with the same token, and a new recording with a new one", async () => {
    fetchMock.mockResolvedValueOnce(reply(500)).mockResolvedValue(reply(202, { note_id: "n1" }));
    renderIt();
    await userEvent.click(screen.getByRole("button", { name: "🎙 add a voice note" }));
    await userEvent.click(screen.getByRole("button", { name: "Save note" }));
    await screen.findByRole("alert");
    await userEvent.click(screen.getByRole("button", { name: "Save note" }));
    await userEvent.click(screen.getByRole("button", { name: "🎙 add a voice note" }));
    await userEvent.click(screen.getByRole("button", { name: "Save note" }));
    const tokens = fetchMock.mock.calls.map(([, init]) => (init.body as FormData).get("client_token"));
    expect(tokens[0]).toBeTruthy();
    expect(tokens[1]).toBe(tokens[0]);
    expect(tokens[2]).not.toBe(tokens[0]);
  });

  it("sends the language picked for this note, and none when it goes with the usual one (#167)", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(reply(202, { note_id: "n1" })));
    renderIt();
    await userEvent.click(screen.getByRole("button", { name: "🎙 add a voice note" }));
    await userEvent.click(screen.getByRole("button", { name: "Save note" }));
    expect((fetchMock.mock.calls[0][1].body as FormData).get("note_language")).toBeNull();
    expect((fetchMock.mock.calls[0][1].body as FormData).get("usual_language")).toBe("en");   // even if its own save failed
    await userEvent.click(await screen.findByRole("button", { name: /Language for this note: English/ }));
    await userEvent.click(screen.getByRole("button", { name: "Kannada" }));
    await userEvent.click(screen.getByRole("button", { name: "🎙 add a voice note" }));
    await userEvent.click(screen.getByRole("button", { name: "Save note" }));
    expect((fetchMock.mock.calls[1][1].body as FormData).get("note_language")).toBe("kn");
    expect(await screen.findByRole("button", { name: /Language for this note: English/ })).toBeInTheDocument();   // back to the usual one
  });
});
