// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import CaptureReview from "./CaptureReview";
import { haptic } from "../haptics";
import { decideSuggestion } from "../api";
import { getCapture, reviewCapture, type CaptureDetail, type CaptureInstance } from "../captureApi";

vi.mock("../haptics", () => ({ haptic: vi.fn() }));
vi.mock("../captureApi", () => ({ getCapture: vi.fn(), reviewCapture: vi.fn() }));
vi.mock("../api", async (original) => ({ ...await original<typeof import("../api")>(), decideSuggestion: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

const instance = (id: string, photo_id: string, track_id: string): CaptureInstance =>
  ({ id, photo_id, track_id, sighting_id: null, species: "dog", thumb_url: `${id}.webp` });
const detail: CaptureDetail = {
  capture_id: "c", processing_state: "ready", captured_at: "2026-10-24T12:00:00Z", note: null, revision: 3,
  sighting_ids: ["s-a", "s-b"], groups: [{ instance_ids: ["a", "c"] }, { instance_ids: ["b"] }],
  instances: [instance("a", "frame1", "t1"), instance("b", "frame1", "t2"), instance("c", "frame2", "t1")],
  suggestions: [
    { id: "name", instance_id: "c", field: "known_name", value: "Kaalu", quote: "The brown one is Kaalu" },
    { id: "hurt", instance_id: "b", field: "condition", value: "injured", quote: "The black one is limping" },
  ],
  voice_transcripts: [{ source: "note", language: "en", text: "The brown one is Kaalu. The black one is limping." }],
};

function open() {
  vi.mocked(getCapture).mockResolvedValue(detail);
  vi.mocked(reviewCapture).mockResolvedValue({ capture_id: "c", processing_state: "ready", sighting_ids: [] });
  vi.mocked(decideSuggestion).mockResolvedValue({ status: "accepted" });
  const onSaved = vi.fn();
  render(<CaptureReview captureId="c" onClose={() => {}} onSaved={onSaved} onUnauthorized={() => {}} />);
  return onSaved;
}

describe("voice-note suggestions in the review sheet", () => {
  it("previews the note with the words behind each open suggestion marked", async () => {
    open();
    const mark = await screen.findByTitle("Name: Kaalu");
    expect(mark.tagName).toBe("MARK");
    expect(mark).toHaveTextContent("The brown one is Kaalu");
    await userEvent.click(within(screen.getByRole("group", { name: "Dog 2" })).getByRole("button", { name: "Not this suggestion for condition" }));
    expect(screen.queryByTitle("Condition: injured")).not.toBeInTheDocument();
  });

  it("says aloud what a used suggestion filled, so the glow is never the only sign", async () => {
    open();
    const first = await screen.findByRole("group", { name: "Dog 1" });
    await userEvent.click(within(first).getByRole("button", { name: "Use Kaalu for name" }));
    expect(screen.getByRole("status")).toHaveTextContent("Filled name with Kaalu.");
    expect(haptic).toHaveBeenCalledWith("filled");
    expect(within(first).getByLabelText("Known name (optional)")).toHaveValue("Kaalu");
  });

  it("keeps marking words behind a detail already saved, and not the ones turned down (#127)", async () => {
    vi.mocked(getCapture).mockResolvedValue({ ...detail, suggestions: [detail.suggestions![1]], groups: [{ instance_ids: ["a", "c"], known_name: "Kaalu" }, { instance_ids: ["b"] }],
      answered_suggestions: [
        { ...detail.suggestions![0], status: "accepted", chosen: "Kaalu" },
        { id: "no", instance_id: "b", field: "sex", value: "male", quote: "limping", status: "dismissed", chosen: null },
      ] });
    render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    const used = await screen.findByTitle("Name: Kaalu (filled in)");
    expect(used).toHaveClass("is-used");
    // The open one is still full strength; the dismissed one is not marked at all.
    expect(screen.getByTitle("Condition: injured")).not.toHaveClass("is-used");
    expect(screen.queryByTitle(/Sex: male/)).not.toBeInTheDocument();
    // Where the used one's words flow: the field it filled, under the animal it was about.
    const first = screen.getByRole("group", { name: "Dog 1" });
    expect(within(first).getByLabelText("Known name (optional)")).toHaveAttribute("data-fx-into", "name");
    expect(within(screen.getByRole("group", { name: "Dog 2" })).getByLabelText("Known name (optional)")).not.toHaveAttribute("data-fx-into");
  });

  it("treats a suggestion the observer already filled in by hand as used, not open", async () => {
    // The raw open list still holds it (older builds read that), and the answered list says it is filled.
    vi.mocked(getCapture).mockResolvedValue({ ...detail, groups: [{ instance_ids: ["a", "c"], known_name: "Kaalu" }, { instance_ids: ["b"] }],
      answered_suggestions: [{ ...detail.suggestions![0], status: "filled", chosen: "Kaalu" }] });
    render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    expect(await screen.findByTitle("Name: Kaalu (filled in)")).toHaveClass("is-used");
    expect(screen.getByTitle("Condition: injured")).not.toHaveClass("is-used");
    expect(screen.getAllByTitle(/Name: Kaalu/)).toHaveLength(1);
  });

  it("offers each suggestion under the animal whose box it was about", async () => {
    open();
    const first = await screen.findByRole("group", { name: "Dog 1" });
    const second = screen.getByRole("group", { name: "Dog 2" });
    expect(within(first).getByRole("button", { name: "Use Kaalu for name" })).toBeInTheDocument();
    expect(within(first).getByRole("radio", { name: "injured" })).not.toHaveAttribute("data-fx-chip");
    expect(within(second).getByRole("radio", { name: /injured/ })).toHaveAttribute("data-fx-chip", "hurt");
    expect(within(second).queryByRole("button", { name: "Use Kaalu for name" })).not.toBeInTheDocument();
  });

  it("fills the blank on use, and records answers only once the sheet is saved", async () => {
    const user = userEvent.setup();
    const onSaved = open();
    const first = await screen.findByRole("group", { name: "Dog 1" });
    await user.click(within(first).getByRole("button", { name: "Use Kaalu for name" }));
    expect(within(first).getByLabelText(/Known name/)).toHaveValue("Kaalu");
    await user.click(within(screen.getByRole("group", { name: "Dog 2" })).getByText("not this"));
    expect(screen.queryByText(/FROM YOUR VOICE NOTE/)).not.toBeInTheDocument();
    expect(decideSuggestion).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Save animal details" }));
    const [, groups] = vi.mocked(reviewCapture).mock.calls[0];
    expect(groups[0]).toMatchObject({ known_name: "Kaalu", instance_ids: ["a", "c"] });
    expect(groups[1].condition).toBeUndefined();
    expect(decideSuggestion).toHaveBeenCalledWith("name", { verdict: "choose", value: "Kaalu" });
    expect(decideSuggestion).toHaveBeenCalledWith("hurt", { verdict: "dismiss" });
    expect(onSaved).toHaveBeenCalled();
  });

  it("records what was finally chosen when the observer changes a used suggestion", async () => {
    const user = userEvent.setup();
    open();
    const second = await screen.findByRole("group", { name: "Dog 2" });
    await user.click(within(second).getByRole("radio", { name: /injured/ }));
    await user.click(within(within(second).getByRole("radiogroup", { name: "Condition" })).getByRole("radio", { name: "unsure" }));
    await user.click(screen.getByRole("button", { name: "Save animal details" }));
    expect(decideSuggestion).toHaveBeenCalledWith("hurt", { verdict: "choose", value: "unsure" });
    expect(decideSuggestion).toHaveBeenCalledTimes(1);
  });

  it("lands the words, and later the glow, on the exact chip a suggestion would fill", async () => {
    const user = userEvent.setup();
    open();
    const second = await screen.findByRole("group", { name: "Dog 2" });
    const chip = within(second).getByRole("radio", { name: /injured/ });
    expect(chip).toHaveAttribute("data-fx-chip", "hurt");
    expect(chip).toHaveClass("suggested");
    expect(within(second).getByRole("radio", { name: "healthy" })).not.toHaveAttribute("data-fx-chip");
    await user.click(chip);
    expect(chip).toBeChecked();
    expect(chip).not.toHaveAttribute("data-fx-chip");
    expect(chip).toHaveAttribute("data-fx-into", "hurt");
    expect(screen.getByRole("status")).toHaveTextContent("Filled condition with injured.");
    expect(haptic).toHaveBeenCalledWith("filled");
    // Moving to another chip takes the glow target with it: it is the chosen value that holds the filled-in mark.
    await user.click(within(within(second).getByRole("radiogroup", { name: "Condition" })).getByRole("radio", { name: "unsure" }));
    expect(chip).not.toHaveAttribute("data-fx-into");
  });

  it("points a saved suggestion's glow at the chip holding its value", async () => {
    vi.mocked(getCapture).mockResolvedValue({ ...detail, suggestions: [detail.suggestions![0]], groups: [{ instance_ids: ["a", "c"] }, { instance_ids: ["b"], condition: "injured" }],
      answered_suggestions: [{ ...detail.suggestions![1], status: "accepted", chosen: "injured" }] });
    render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    const second = await screen.findByRole("group", { name: "Dog 2" });
    expect(within(second).getByRole("radio", { name: "injured" })).toHaveAttribute("data-fx-into", "hurt");
    expect(within(second).getByRole("radio", { name: "healthy" })).not.toHaveAttribute("data-fx-into");
  });

  it("ticks lightly on a chip pick or clear, buzzes success only when Save succeeds, and stays quiet on failure", async () => {
    const user = userEvent.setup();
    open();
    const first = await screen.findByRole("group", { name: "Dog 1" });
    expect(haptic).not.toHaveBeenCalled(); // opening, and the glow, are silent here
    const sex = within(within(first).getByRole("radiogroup", { name: "Sex" }));
    await user.click(sex.getByRole("radio", { name: "♀ female" }));
    expect(haptic).toHaveBeenLastCalledWith("tick");
    await user.click(sex.getByRole("radio", { name: "♀ female" }));
    expect(haptic).toHaveBeenCalledTimes(2);
    await user.type(within(first).getByLabelText(/Known name/), "Raja");
    expect(haptic).toHaveBeenCalledTimes(2);
    vi.mocked(reviewCapture).mockRejectedValueOnce(new Error("down"));
    await user.click(screen.getByRole("button", { name: "Save animal details" }));
    expect(haptic).toHaveBeenCalledTimes(2);
    await user.click(screen.getByRole("button", { name: "Reload" }));
    vi.mocked(reviewCapture).mockResolvedValue({ capture_id: "c", processing_state: "ready", sighting_ids: [] });
    await user.click(await screen.findByRole("button", { name: "Save animal details" }));
    expect(haptic).toHaveBeenLastCalledWith("logged");
  });
});
