// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import CaptureReview from "./CaptureReview";
import { decideSuggestion } from "../api";
import { getCapture, reviewCapture, type CaptureDetail, type CaptureInstance } from "../captureApi";

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
    await userEvent.click(within(screen.getByRole("group", { name: "Dog 2" })).getByRole("button", { name: "not this" }));
    expect(screen.queryByTitle("Condition: injured")).not.toBeInTheDocument();
  });

  it("offers each suggestion under the animal whose box it was about", async () => {
    open();
    const first = await screen.findByRole("group", { name: "Dog 1" });
    const second = screen.getByRole("group", { name: "Dog 2" });
    expect(within(first).getByText("“The brown one is Kaalu”")).toBeInTheDocument();
    expect(within(first).queryByText(/limping/)).not.toBeInTheDocument();
    expect(within(second).getByText("“The black one is limping”")).toBeInTheDocument();
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
    await user.click(within(second).getByRole("button", { name: "Use injured for condition" }));
    await user.selectOptions(within(second).getByLabelText("Condition"), "unsure");
    await user.click(screen.getByRole("button", { name: "Save animal details" }));
    expect(decideSuggestion).toHaveBeenCalledWith("hurt", { verdict: "choose", value: "unsure" });
    expect(decideSuggestion).toHaveBeenCalledTimes(1);
  });
});
