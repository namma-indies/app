// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import CaptureReview, { groupingError } from "./CaptureReview";
import { getCapture, reviewCapture, type CaptureDetail, type CaptureInstance } from "../captureApi";
vi.mock("../captureApi", () => ({ getCapture: vi.fn(), reviewCapture: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
const instance = (id: string, photo_id: string, track_id = id): CaptureInstance => ({ id, photo_id, track_id, sighting_id: null, species: "dog", thumb_url: `${id}.webp` });
const detail: CaptureDetail = { capture_id: "c", processing_state: "needs_review", captured_at: "2026-09-15T12:00:00Z", note: "shared note", revision: 2, sighting_ids: [], groups: [{ instance_ids: ["a", "c"] }, { instance_ids: ["b"] }], instances: [instance("a", "frame1", "track1"), instance("b", "frame1", "track2"), instance("c", "frame2", "track1")] };

describe("private grouping review", () => {
  it("rejects co-visible merges and incomplete assignments", () => {
    expect(groupingError(detail.instances, { a: "1", b: "1", c: "2" })).toMatch(/visible together/);
    expect(groupingError(detail.instances, { a: "1" })).toMatch(/every/);
    expect(groupingError(detail.instances, { a: "1", b: "2", c: "1" })).toBeNull();
  });
  it("lets a contributor split fragments, join them to another animal, and publish separate details", async () => {
    vi.mocked(getCapture).mockResolvedValue(detail);
    vi.mocked(reviewCapture).mockResolvedValue({ capture_id: "c", sighting_ids: ["s1", "s2"], processing_state: "ready" });
    const onSaved = vi.fn();
    render(<CaptureReview captureId="c" onClose={() => {}} onSaved={onSaved} onUnauthorized={() => {}} />);
    await screen.findByText("Details for each animal");
    await userEvent.selectOptions(screen.getByLabelText("Group for evidence 3"), "3");
    expect(screen.getByRole("button", { name: "Publish 3 animal sightings" })).toBeEnabled();
    await userEvent.selectOptions(screen.getByLabelText("Group for evidence 3"), "2");
    await userEvent.selectOptions(screen.getAllByLabelText("sex")[0], "female");
    await userEvent.selectOptions(screen.getAllByLabelText("condition")[1], "injured");
    await userEvent.click(screen.getByRole("button", { name: "Publish 2 animal sightings" }));
    expect(reviewCapture).toHaveBeenCalledWith("c", [{ instance_ids: ["a"], sex: "female" }, { instance_ids: ["b", "c"], condition: "injured" }], 2);
    expect(onSaved).toHaveBeenCalledOnce();
  });
  it("blocks publication of co-visible animals assigned to the same group", async () => {
    vi.mocked(getCapture).mockResolvedValue(detail);
    render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    await screen.findByText("Details for each animal");
    await userEvent.selectOptions(screen.getByLabelText("Group for evidence 2"), "1");
    expect(screen.getByRole("alert")).toHaveTextContent("visible together");
    expect(screen.getByRole("button", { name: "Publish 1 animal sighting" })).toBeDisabled();
    expect(reviewCapture).not.toHaveBeenCalled();
  });
  it("preserves unsaved work when the parent changes its authorization callback", async () => {
    vi.mocked(getCapture).mockResolvedValue(detail);
    const props = { captureId: "c", onClose: vi.fn(), onSaved: vi.fn() };
    const { rerender } = render(<CaptureReview {...props} onUnauthorized={() => {}} />);
    await screen.findByText("Details for each animal");
    await userEvent.selectOptions(screen.getByLabelText("Group for evidence 3"), "3");
    await userEvent.selectOptions(screen.getAllByLabelText("sex")[0], "female");
    rerender(<CaptureReview {...props} onUnauthorized={() => {}} />);
    expect(getCapture).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText("Group for evidence 3")).toHaveValue("3");
    expect(screen.getAllByLabelText("sex")[0]).toHaveValue("female");
  });
  it("requires fresh evidence after a lost save acknowledgement before submitting again", async () => {
    vi.mocked(getCapture).mockResolvedValueOnce(detail).mockResolvedValue({ ...detail, revision: 3, processing_state: "ready" });
    vi.mocked(reviewCapture).mockRejectedValue(new TypeError("lost response"));
    render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Publish 2 animal sightings" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Reload evidence");
    expect(screen.getByRole("button", { name: "Publish 2 animal sightings" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Reload evidence" }));
    expect(await screen.findByRole("button", { name: "Save animal details" })).toBeEnabled();
    expect(screen.getByLabelText("Group for evidence 1")).toBeDisabled();
    expect(reviewCapture).toHaveBeenCalledOnce();
  });
  it.each(["processing", "failed", "no_animal"] as const)("does not publish evidence in state %s", async (processing_state) => {
    vi.mocked(getCapture).mockResolvedValue({ ...detail, processing_state });
    render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    expect(await screen.findByRole("button", { name: "Publish 2 animal sightings" })).toBeDisabled();
    expect(reviewCapture).not.toHaveBeenCalled();
  });
  it("locks published grouping while preserving editable per-animal details", async () => {
    vi.mocked(getCapture).mockResolvedValue({ ...detail, processing_state: "ready" });
    render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    await waitFor(() => expect(screen.getByLabelText("Group for evidence 1")).toBeDisabled());
    expect(screen.getAllByLabelText("sex")[0]).toBeEnabled();
    expect(screen.getByRole("button", { name: "Save animal details" })).toBeEnabled();
  });
});
