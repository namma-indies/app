// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  getModerationQueue: vi.fn(), getAnimalAudit: vi.fn(), getAnimalAuditDetail: vi.fn(),
  ruleOnAnimal: vi.fn(), ruleOnCaptureAnimal: vi.fn(),
}));
import {
  getAnimalAudit, getAnimalAuditDetail, getModerationQueue, HttpError, ruleOnAnimal,
  ruleOnCaptureAnimal, UnauthorizedError, type AnimalAuditDetail, type AnimalAuditItem, type AnimalAuditPage,
} from "../api";
import Moderation from "./Moderation";

const item = (over: Partial<AnimalAuditItem> = {}): AnimalAuditItem => ({
  id: "s1", kind: "sighting", captured_at: "2026-08-01T10:00:00Z", processing_state: "ready",
  animal_override: null, dog: 0.03, cat: 0.82, thumb_url: "thumb.webp", ...over,
});
const detail = (over: Partial<AnimalAuditDetail> = {}): AnimalAuditDetail => ({
  ...item(), clip_url: null,
  evidence: [{ id: "photo1", evidence_kind: "source", photo_url: "full.webp", thumb_url: "thumb.webp", width: 200, height: 100,
    dog: 0.03, cat: 0.82, model: "yolo-test", boxes: [
      { bbox: [20, 10, 100, 90], species: "cat", confidence: 0.82 },
      { bbox: [110, 20, 190, 90], species: "dog", confidence: 0.03 },
    ] }], ...over,
});
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((yes) => { resolve = yes; });
  return { promise, resolve };
}
async function openAudit(onUnauthorized = vi.fn()) {
  render(<Moderation onUnauthorized={onUnauthorized} />);
  await userEvent.click(screen.getByRole("button", { name: "DETECTOR AUDIT" }));
}
async function openDetail() {
  await openAudit();
  await userEvent.click(await screen.findByRole("button", { name: "Open evidence" }));
  await screen.findByText(/Item model scores/);
}
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(getModerationQueue).mockResolvedValue({ items: [] });
  vi.mocked(getAnimalAudit).mockResolvedValue({ items: [item()], next_cursor: null });
  vi.mocked(getAnimalAuditDetail).mockResolvedValue(detail());
  vi.mocked(ruleOnAnimal).mockResolvedValue(undefined);
  vi.mocked(ruleOnCaptureAnimal).mockResolvedValue(undefined);
});
afterEach(cleanup);

describe("Moderation detector audit", () => {
  it("defaults to all and shows model scores separately from human review", async () => {
    await openAudit();
    expect(await screen.findByText("Model: DOG 0.03 · CAT 0.82")).toBeInTheDocument();
    expect(screen.getByText("Human verdict: Not reviewed")).toBeInTheDocument();
    expect(getAnimalAudit).toHaveBeenCalledWith("all", null, expect.any(AbortSignal));
  });

  it("filters on the server, paginates in server order and keeps same IDs of different kinds", async () => {
    vi.mocked(getAnimalAudit).mockResolvedValueOnce({ items: [item()], next_cursor: "opaque+/=" })
      .mockResolvedValueOnce({ items: [item({ kind: "capture", dog: 0.1 })], next_cursor: null })
      .mockResolvedValue({ items: [], next_cursor: null });
    await openAudit();
    await userEvent.click(await screen.findByRole("button", { name: "Load more" }));
    expect(getAnimalAudit).toHaveBeenLastCalledWith("all", "opaque+/=", expect.any(AbortSignal));
    await waitFor(() => expect(screen.getAllByRole("button", { name: "Open evidence" })).toHaveLength(2));
    const cards = screen.getAllByRole("article");
    expect(cards[0]).toHaveTextContent("Model: DOG 0.03");
    expect(cards[1]).toHaveTextContent("Model: DOG 0.10");
    for (const [label, filter] of [["Needs review", "needs_review"], ["Reviewed", "reviewed"], ["No detection", "no_detection"]]) {
      await userEvent.click(screen.getByRole("button", { name: label }));
      expect(await screen.findByText("No items in this filter.")).toBeInTheDocument();
      expect(getAnimalAudit).toHaveBeenLastCalledWith(filter, null, expect.any(AbortSignal));
    }
  });

  it("does not turn missing scores, failed processing or missing boxes into no detection", async () => {
    vi.mocked(getAnimalAudit).mockResolvedValue({ items: [
      item({ id: "none", processing_state: "no_animal", dog: null, cat: null }),
      item({ id: "legacy", processing_state: "legacy", dog: null, cat: null }),
      item({ id: "failed", processing_state: "failed", dog: null, cat: null }),
      item({ id: "pending", processing_state: "processing", dog: null, cat: null }),
    ], next_cursor: null });
    await openAudit();
    expect(await screen.findByText("No detection — model result, not a human verdict")).toBeInTheDocument();
    expect(screen.getByText("Unscored — results unavailable")).toBeInTheDocument();
    expect(screen.getByText("Processing failed — not evidence of absence")).toBeInTheDocument();
    expect(screen.getByText("Processing — results incomplete")).toBeInTheDocument();
  });

  it("shows larger full evidence, numbered species boxes, every frame and optional clip", async () => {
    const first = detail().evidence[0];
    vi.mocked(getAnimalAuditDetail).mockResolvedValue(detail({ clip_url: "clip.mp4", evidence: [first,
      { ...first, id: "photo2", photo_url: "second.webp", boxes: [], dog: null, cat: null, model: null }] }));
    await openDetail();
    expect(screen.getByRole("img", { name: "Evidence frame 1" })).toHaveAttribute("src", "full.webp");
    expect(screen.getByText("Source photo")).toBeInTheDocument();
    expect(screen.getByText(/Review only this sighting's animal/)).toBeInTheDocument();
    expect(screen.getByText(/hides this sighting from shared surfaces/)).toBeInTheDocument();
    expect(screen.getByText("1 CAT").parentElement).toHaveStyle({ left: "10%", top: "10%", width: "40%", height: "80%" });
    expect(screen.getByText("Box 2: dog · model confidence 0.03")).toBeInTheDocument();
    expect(screen.getByLabelText("Original capture clip")).toHaveAttribute("src", "clip.mp4");
    await userEvent.click(screen.getByRole("button", { name: "Next frame" }));
    expect(screen.getByText("Frame 2 of 2")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Evidence frame 2" })).toHaveAttribute("src", "second.webp");
    expect(screen.getByText(/No displayable dog\/cat boxes/)).toBeInTheDocument();
    expect(screen.getByText("Frame model: unavailable · DOG not scored · CAT not scored")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Previous frame" }));
    expect(screen.getByText("Frame 1 of 2")).toBeInTheDocument();
  });

  it("records reversible sighting verdicts without altering model scores", async () => {
    await openDetail();
    await userEvent.click(screen.getByRole("button", { name: "ANIMAL PRESENT" }));
    expect(ruleOnAnimal).toHaveBeenCalledWith("s1", "animal");
    expect(await screen.findByText("Human verdict: Animal present")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "NO ANIMAL" }));
    expect(ruleOnAnimal).toHaveBeenLastCalledWith("s1", "no_animal");
    expect(await screen.findByText("Human verdict: No animal")).toBeInTheDocument();
    expect(screen.getByText("Item model scores: DOG 0.03 · CAT 0.82")).toBeInTheDocument();
    expect(ruleOnCaptureAnimal).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Back to audit" }));
    expect(screen.getByText("Human verdict: No animal")).toBeInTheDocument();
  });

  it("uses the capture-only review endpoint and explains that review creates no detection", async () => {
    vi.mocked(getAnimalAudit).mockResolvedValue({ items: [item({ kind: "capture" })], next_cursor: null });
    vi.mocked(getAnimalAuditDetail).mockResolvedValue(detail({ kind: "capture" }));
    await openDetail();
    expect(screen.getByText(/only records your review; it does not create a detection or sighting/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "ANIMAL PRESENT" }));
    expect(ruleOnCaptureAnimal).toHaveBeenCalledWith("s1", "animal");
    expect(ruleOnAnimal).not.toHaveBeenCalled();
  });

  it("skip advances locally and never persists a verdict", async () => {
    vi.mocked(getAnimalAudit).mockResolvedValue({ items: [item(), item({ id: "s2" })], next_cursor: null });
    await openAudit();
    await userEvent.click((await screen.findAllByRole("button", { name: "Open evidence" }))[0]);
    await screen.findByText(/Item model scores/);
    await userEvent.click(screen.getByRole("button", { name: "Unsure / skip" }));
    expect(getAnimalAuditDetail).toHaveBeenLastCalledWith("sighting", "s2", expect.any(AbortSignal));
    expect(ruleOnAnimal).not.toHaveBeenCalled();
    expect(ruleOnCaptureAnimal).not.toHaveBeenCalled();
  });

  it("ignores a late old-filter result even if the transport ignores cancellation", async () => {
    const old = deferred<AnimalAuditPage>();
    vi.mocked(getAnimalAudit).mockReturnValueOnce(old.promise).mockResolvedValue({ items: [], next_cursor: null });
    await openAudit();
    const signal = vi.mocked(getAnimalAudit).mock.calls[0][2]!;
    await userEvent.click(screen.getByRole("button", { name: "Reviewed" }));
    await screen.findByText("No items in this filter.");
    expect(signal.aborted).toBe(true);
    await act(async () => old.resolve({ items: [item()], next_cursor: "stale" }));
    expect(screen.queryByRole("button", { name: "Open evidence" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });

  it("ignores late evidence after skipping to another item", async () => {
    const old = deferred<AnimalAuditDetail>();
    vi.mocked(getAnimalAudit).mockResolvedValue({ items: [item(), item({ id: "s2" })], next_cursor: null });
    vi.mocked(getAnimalAuditDetail).mockReturnValueOnce(old.promise).mockResolvedValue(detail({ id: "s2", dog: 0.99 }));
    await openAudit();
    await userEvent.click((await screen.findAllByRole("button", { name: "Open evidence" }))[0]);
    const signal = vi.mocked(getAnimalAuditDetail).mock.calls[0][2]!;
    await userEvent.click(screen.getByRole("button", { name: "Unsure / skip" }));
    expect(await screen.findByText("Item model scores: DOG 0.99 · CAT 0.82")).toBeInTheDocument();
    expect(signal.aborted).toBe(true);
    await act(async () => old.resolve(detail()));
    expect(screen.queryByText("Item model scores: DOG 0.03 · CAT 0.82")).not.toBeInTheDocument();
  });

  it("keeps Reported accessible through audit failures and offers direct retry", async () => {
    vi.mocked(getAnimalAudit).mockRejectedValueOnce(new Error("offline")).mockResolvedValue({ items: [], next_cursor: null });
    await openAudit();
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load the audit");
    await userEvent.click(screen.getByRole("button", { name: "Retry list" }));
    expect(await screen.findByText("No items in this filter.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "REPORTED" }));
    expect(screen.getByText(/NOTHING REPORTED/)).toBeInTheDocument();
  });

  it("retries failed pagination without losing loaded items", async () => {
    vi.mocked(getAnimalAudit).mockResolvedValueOnce({ items: [item()], next_cursor: "next" })
      .mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce({ items: [item({ id: "s2" })], next_cursor: null });
    await openAudit();
    await userEvent.click(await screen.findByRole("button", { name: "Load more" }));
    await screen.findByRole("alert");
    expect(screen.getAllByRole("button", { name: "Open evidence" })).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: "Retry list" }));
    await waitFor(() => expect(screen.getAllByRole("button", { name: "Open evidence" })).toHaveLength(2));
    expect(getAnimalAudit).toHaveBeenLastCalledWith("all", "next", expect.any(AbortSignal));
  });

  it("retries failed detail loads", async () => {
    vi.mocked(getAnimalAuditDetail).mockRejectedValueOnce(new Error("offline")).mockResolvedValue(detail());
    await openAudit();
    await userEvent.click(await screen.findByRole("button", { name: "Open evidence" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't load the evidence");
    await userEvent.click(screen.getByRole("button", { name: "Refresh evidence" }));
    expect(await screen.findByText(/Item model scores/)).toBeInTheDocument();
  });

  it("requires refreshed evidence after a stale 409 without claiming a saved verdict", async () => {
    vi.mocked(ruleOnAnimal).mockRejectedValueOnce(new HttpError(409));
    await openDetail();
    await userEvent.click(screen.getByRole("button", { name: "ANIMAL PRESENT" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("your verdict was not saved");
    expect(screen.getByRole("button", { name: "NO ANIMAL" })).toBeDisabled();
    expect(screen.getByText("Human verdict: Not reviewed")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Refresh evidence" }));
    expect(await screen.findByRole("button", { name: "NO ANIMAL" })).toBeEnabled();
    expect(getAnimalAuditDetail).toHaveBeenCalledTimes(2);
  });

  it("prevents duplicate saves and leaves failed verdicts retryable", async () => {
    const save = deferred<void>();
    vi.mocked(ruleOnAnimal).mockReturnValueOnce(save.promise).mockRejectedValueOnce(new Error("offline"));
    await openDetail();
    await userEvent.click(screen.getByRole("button", { name: "ANIMAL PRESENT" }));
    expect(screen.getByRole("button", { name: "NO ANIMAL" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Back to audit" })).toBeDisabled();
    await act(async () => save.resolve());
    await userEvent.click(screen.getByRole("button", { name: "NO ANIMAL" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't save the verdict");
    expect(screen.getByText("Human verdict: Animal present")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "NO ANIMAL" })).toBeEnabled();
  });

  it("forwards an expired session", async () => {
    const unauthorized = vi.fn();
    vi.mocked(getAnimalAudit).mockRejectedValue(new UnauthorizedError());
    await openAudit(unauthorized);
    await waitFor(() => expect(unauthorized).toHaveBeenCalledOnce());
  });
});
