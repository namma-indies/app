// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { getMe, setNoteLanguage, type NoteLanguage } from "../api";
import NoteLanguagePicker, { forgetNoteLanguage, useNoteLanguage } from "./NoteLanguagePicker";

vi.mock("../api", async (original) => ({ ...await original<typeof import("../api")>(), getMe: vi.fn(), setNoteLanguage: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

function Harness({ usual: start }: { usual: NoteLanguage | null | undefined }) {
  const [usual, setUsual] = useState(start);
  const [chosen, setChosen] = useState<NoteLanguage | null>(null);
  return <>
    <NoteLanguagePicker usual={usual} onKeep={setUsual} chosen={chosen} onChoose={setChosen} />
    <output data-testid="chosen">{chosen ?? "usual"}</output>
  </>;
}

describe("which language a note is spoken in (#167)", () => {
  it("asks once, with every language offered, until answered", async () => {
    render(<Harness usual={null} />);
    expect(screen.getByText("What language do you usually speak your notes in?")).toBeInTheDocument();
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual(
      ["English", "Kannada", "Hindi", "Tamil", "Telugu", "Malayalam", "Mixed / let the app decide"]);
    await userEvent.click(screen.getByRole("button", { name: "Kannada" }));
    expect(screen.queryByText("What language do you usually speak your notes in?")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Language for this note: Kannada/ })).toHaveTextContent("KN ▾");
  });

  it("changes the language for one note only, and going back to the usual one is no choice at all", async () => {
    render(<Harness usual="en" />);
    const chip = screen.getByRole("button", { name: /Language for this note: English/ });
    expect(chip).toHaveTextContent("EN ▾");
    await userEvent.click(chip);
    await userEvent.click(screen.getByRole("button", { name: "Hindi" }));
    expect(screen.getByTestId("chosen")).toHaveTextContent("hi");
    expect(screen.getByRole("button", { name: /Language for this note: Hindi/ })).toHaveTextContent("HI ▾");
    await userEvent.click(screen.getByRole("button", { name: /Language for this note/ }));
    await userEvent.click(screen.getByRole("button", { name: "English" }));
    expect(screen.getByTestId("chosen")).toHaveTextContent("usual");
  });

  it("shows mixed when it cannot tell, and asks nothing", () => {
    render(<Harness usual={undefined} />);
    expect(screen.queryByText("What language do you usually speak your notes in?")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Language for this note: Mixed/ })).toHaveTextContent("MIXED ▾");
  });

  it("reads the answer from /me and keeps a new one", async () => {
    vi.mocked(getMe).mockResolvedValue({ id: "o", display_name: null, is_moderator: false, note_language: null });
    vi.mocked(setNoteLanguage).mockResolvedValue({ note_language: "ta" });
    const { result } = renderHook(() => useNoteLanguage());
    await waitFor(() => expect(result.current.usual).toBeNull());               // never answered: ask
    await result.current.keep("ta");
    expect(setNoteLanguage).toHaveBeenCalledWith("ta");
    await waitFor(() => expect(result.current.usual).toBe("ta"));
  });

  it("keeps an answer whose save failed, shows it after a reload, and saves it then (#168 review)", async () => {
    localStorage.clear();
    vi.mocked(getMe).mockResolvedValue({ id: "o", display_name: null, is_moderator: false, note_language: null });
    vi.mocked(setNoteLanguage).mockRejectedValueOnce(new Error("offline"));
    const first = renderHook(() => useNoteLanguage());
    await waitFor(() => expect(first.result.current.usual).toBeNull());
    await first.result.current.keep("kn");
    first.unmount();
    vi.mocked(setNoteLanguage).mockResolvedValue({ note_language: "kn" });
    const again = renderHook(() => useNoteLanguage());
    await waitFor(() => expect(again.result.current.usual).toBe("kn"));        // not asked again
    await waitFor(() => expect(setNoteLanguage).toHaveBeenLastCalledWith("kn")); // and saved this time
    await waitFor(() => expect(localStorage.getItem("indiedex.noteLanguage.unsaved")).toBeNull());
  });

  it("never hands one person's unsaved answer to the next person signed in (#168 review)", async () => {
    localStorage.clear();
    vi.mocked(getMe).mockResolvedValueOnce({ id: "a", display_name: null, is_moderator: false, note_language: null });
    vi.mocked(setNoteLanguage).mockRejectedValueOnce(new Error("offline"));
    const a = renderHook(() => useNoteLanguage());
    await waitFor(() => expect(a.result.current.usual).toBeNull());
    await a.result.current.keep("kn");
    a.unmount();                                                                 // A signs out; B signs in
    vi.mocked(setNoteLanguage).mockClear();
    vi.mocked(getMe).mockResolvedValue({ id: "b", display_name: null, is_moderator: false, note_language: null });
    const b = renderHook(() => useNoteLanguage());
    await waitFor(() => expect(b.result.current.usual).toBeNull());             // B is asked
    expect(setNoteLanguage).not.toHaveBeenCalled();                              // nothing of A's saved onto B
    expect(localStorage.getItem("indiedex.noteLanguage.unsaved")).toBeNull();
  });

  it("lets go of a kept answer once the server has one, and never uses it while it cannot tell who is signed in", async () => {
    localStorage.clear();
    vi.mocked(getMe).mockResolvedValueOnce({ id: "a", display_name: null, is_moderator: false, note_language: null });
    vi.mocked(setNoteLanguage).mockRejectedValueOnce(new Error("offline"));
    const first = renderHook(() => useNoteLanguage());
    await waitFor(() => expect(first.result.current.usual).toBeNull());
    await first.result.current.keep("ta");
    first.unmount();
    vi.mocked(getMe).mockRejectedValueOnce(new TypeError("Failed to fetch"));    // offline: who is this?
    const offline = renderHook(() => useNoteLanguage());
    await waitFor(() => expect(getMe).toHaveBeenCalledTimes(2));
    expect(offline.result.current.usual).toBeUndefined();                       // nothing shown, so nothing sent
    offline.unmount();
    vi.mocked(getMe).mockResolvedValueOnce({ id: "a", display_name: null, is_moderator: false, note_language: "ta" });
    const online = renderHook(() => useNoteLanguage());
    await waitFor(() => expect(online.result.current.usual).toBe("ta"));
    expect(localStorage.getItem("indiedex.noteLanguage.unsaved")).toBeNull();   // the server has it now
  });

  it("does not hand A's unsaved answer to B when B's /me fails, and sign-out forgets it (#168 review)", async () => {
    localStorage.clear();
    vi.mocked(getMe).mockResolvedValueOnce({ id: "a", display_name: null, is_moderator: false, note_language: null });
    vi.mocked(setNoteLanguage).mockRejectedValueOnce(new Error("offline"));
    const a = renderHook(() => useNoteLanguage());
    await waitFor(() => expect(a.result.current.usual).toBeNull());
    await a.result.current.keep("kn");
    a.unmount();
    vi.mocked(setNoteLanguage).mockClear();
    vi.mocked(getMe).mockRejectedValueOnce(new TypeError("Failed to fetch"));    // B signed in; B's /me fails
    const b = renderHook(() => useNoteLanguage());
    await waitFor(() => expect(getMe).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 20));
    expect(b.result.current.usual).toBeUndefined();                              // no A language shown, none sent
    expect(setNoteLanguage).not.toHaveBeenCalled();
    forgetNoteLanguage();                                                         // what sign-out does
    expect(localStorage.getItem("indiedex.noteLanguage.unsaved")).toBeNull();
  });

  it("does not ask again because saving the answer failed", async () => {
    vi.mocked(getMe).mockResolvedValue({ id: "o", display_name: null, is_moderator: false, note_language: null });
    vi.mocked(setNoteLanguage).mockRejectedValue(new Error("offline"));
    const { result } = renderHook(() => useNoteLanguage());
    await waitFor(() => expect(result.current.usual).toBeNull());
    await result.current.keep("hi");
    await waitFor(() => expect(result.current.usual).toBe("hi"));
  });
});
