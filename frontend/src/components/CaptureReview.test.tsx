// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import CaptureReview, { groupingError } from "./CaptureReview";
import { getCapture, reviewCapture, type CaptureDetail, type CaptureInstance } from "../captureApi";
vi.mock("../captureApi", () => ({ getCapture: vi.fn(), reviewCapture: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
const instance = (id: string, photo_id: string, track_id = id): CaptureInstance => ({ id, photo_id, track_id, sighting_id: null, species: "dog", thumb_url: `${id}.webp` });
const detail: CaptureDetail = { capture_id: "c", processing_state: "needs_review", captured_at: "2026-09-15T12:00:00Z", note: "shared note", revision: 2, sighting_ids: [], groups: [{ instance_ids: ["a", "c"] }, { instance_ids: ["b"] }], instances: [instance("a", "frame1", "track1"), instance("b", "frame1", "track2"), instance("c", "frame2", "track1")] };

describe("private grouping review", () => {
  it("groups uncropped overviews by source and selects identical boxes through keyboard and touch controls", async () => {
    const user = userEvent.setup();
    vi.mocked(getCapture).mockResolvedValue({ ...detail, processing_state: "ready", instances: detail.instances.map((item, index) => ({
      ...item, source_thumb_url: `${item.photo_id}_thumb.webp`, source_width: 200, source_height: 400,
      source_bbox: [0.1, 0.2, 0.5, 0.6], bbox: [0.05, 0.05, 0.95, 0.95], source_url: "crop_full.webp", timestamp_ms: index === 2 ? 1500 : 0,
    })) });
    const { container } = render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    const first = await screen.findByRole("region", { name: "Source photo 1" });
    const second = screen.getByRole("region", { name: "Source photo 2" });
    expect(within(first).getAllByRole("article")).toHaveLength(2);
    expect(within(second).getAllByRole("article")).toHaveLength(1);
    expect(within(second).getByRole("heading")).toHaveTextContent("1.5s");
    const photo = within(first).getByAltText("Full uncropped source photo 1");
    expect(photo).toHaveAttribute("src", "frame1_thumb.webp");
    expect(photo).toHaveAttribute("width", "200");
    expect(photo).toHaveAttribute("height", "400");
    expect(container.querySelectorAll(".capture-source-image img")).toHaveLength(2);
    expect(container.querySelector('img[src="crop_full.webp"]')).toBeNull();
    const boxes = container.querySelectorAll(".capture-source-box");
    expect(boxes).toHaveLength(3);
    expect(boxes[0]).toHaveStyle({ left: "10%", top: "20%", width: "40%" });
    expect(boxes[0].getAttribute("style")).not.toBe(boxes[1].getAttribute("style"));
    expect(boxes[0]).toHaveTextContent("1");
    expect(boxes[1]).toHaveTextContent("2");
    const firstButton = within(first).getByRole("button", { name: /Evidence 1/ });
    const overlappingButton = within(first).getByRole("button", { name: /Evidence 2/ });
    firstButton.focus();
    await user.tab();
    expect(overlappingButton).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(overlappingButton).toHaveAttribute("aria-pressed", "true");
    expect(firstButton).toHaveAttribute("aria-pressed", "false");
    expect(boxes[1]).toHaveClass("is-selected");
    expect(boxes[0]).not.toHaveClass("is-selected");
    expect(within(first).getByRole("article", { name: "Evidence 2" })).toHaveClass("is-selected");
    expect(screen.getByRole("group", { name: "Dog 2" })).toHaveClass("capture-entry-selected");
    await user.pointer([{ keys: "[TouchA>]", target: firstButton }, { keys: "[/TouchA]" }]);
    expect(firstButton).toHaveAttribute("aria-pressed", "true");
    expect(boxes[0]).toHaveClass("is-selected");
    overlappingButton.focus();
    await user.keyboard(" ");
    expect(overlappingButton).toHaveAttribute("aria-pressed", "true");
    expect(boxes[1]).toHaveClass("is-selected");
    expect(reviewCapture).not.toHaveBeenCalled();
    expect(screen.getByText(/detections, not confirmed identities/)).toBeInTheDocument();
  });
  it("numbers groups separately by species across boxes, evidence, options and details", async () => {
    const instances = [detail.instances[0], { ...instance("cat", "cat-frame"), species: "cat" }, detail.instances[1], detail.instances[2]].map((item) => ({
      ...item, source_thumb_url: `${item.photo_id}_thumb.webp`, source_bbox: [0.1, 0.2, 0.5, 0.6] as [number, number, number, number],
    }));
    vi.mocked(getCapture).mockResolvedValue({ ...detail, instances, groups: [{ instance_ids: ["a", "c"] }, { instance_ids: ["cat"] }, { instance_ids: ["b"] }] });
    const { container } = render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    await screen.findByRole("group", { name: "Cat 1" });
    for (const [evidence, label] of [[1, "Dog 1"], [2, "Cat 1"], [3, "Dog 2"], [4, "Dog 1"]] as const) {
      const card = screen.getByRole("article", { name: `Evidence ${evidence}` });
      expect(within(card).getByRole("button")).toHaveTextContent(`Evidence ${evidence} · ${label}`);
      expect(within(card).getByRole("combobox")).toHaveDisplayValue(label);
      expect(screen.getByRole("group", { name: label })).toBeInTheDocument();
      expect(container.querySelector(`#capture-box-${instances[evidence - 1].id}`)).toHaveTextContent(`${label} · Evidence ${evidence}`);
    }
    const dogSelect = screen.getByLabelText("Group for evidence 1");
    const catSelect = screen.getByLabelText("Group for evidence 2");
    expect(within(dogSelect).queryByRole("option", { name: "Cat 1" })).not.toBeInTheDocument();
    expect(within(catSelect).queryByRole("option", { name: /Dog/ })).not.toBeInTheDocument();
    expect(within(dogSelect).getByRole("option", { name: "New dog group" })).toBeInTheDocument();
    expect(within(catSelect).getByRole("option", { name: "New cat group" })).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Group for evidence 4"), "4");
    expect(screen.getByRole("group", { name: "Dog 3" })).toHaveTextContent("Evidence 4");
    expect(container.querySelector("#capture-box-c")).toHaveTextContent("Dog 3 · Evidence 4");
    await userEvent.selectOptions(screen.getByLabelText("Group for evidence 4"), "3");
    expect(screen.queryByRole("group", { name: "Dog 3" })).not.toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Dog 2" })).toHaveTextContent("Evidence 3, 4");
    expect(container.querySelector("#capture-box-c")).toHaveTextContent("Dog 2 · Evidence 4");
    expect(screen.getByLabelText("Group for evidence 2")).toHaveDisplayValue("Cat 1");
  });
  it("marks mixed-species groups invalid without labeling them as a dog or cat", async () => {
    const instances = [instance("a", "dog-frame"), { ...instance("cat", "cat-frame"), species: "cat" }, instance("b", "other-dog-frame")];
    vi.mocked(getCapture).mockResolvedValue({ ...detail, instances, groups: [{ instance_ids: ["a", "cat"] }, { instance_ids: ["b"] }] });
    render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    const mixed = "Mixed species group 1 (split required)";
    expect(await screen.findByRole("group", { name: mixed })).toHaveTextContent("Evidence 1, 2");
    expect(screen.getByRole("alert")).toHaveTextContent("Different species");
    expect(screen.getByRole("button", { name: "Publish 2 animal sightings" })).toBeDisabled();
    expect(screen.getByLabelText("Group for evidence 1")).toHaveDisplayValue(mixed);
    expect(within(screen.getByLabelText("Group for evidence 1")).getByRole("option", { name: mixed })).toBeDisabled();
    expect(within(screen.getByLabelText("Group for evidence 3")).queryByRole("option", { name: mixed })).not.toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Group for evidence 2"), "3");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Cat 1" })).toHaveTextContent("Evidence 2");
    expect(screen.getByLabelText("Group for evidence 1")).toHaveDisplayValue("Dog 1");
    expect(screen.getByLabelText("Group for evidence 3")).toHaveDisplayValue("Dog 2");
    expect(screen.getByRole("button", { name: "Publish 3 animal sightings" })).toBeEnabled();
  });
  it("keeps legacy crop evidence and crop-relative boxes when source dimensions are absent", async () => {
    vi.mocked(getCapture).mockResolvedValue({ ...detail, instances: [{ ...detail.instances[0], source_url: "legacy_crop.webp", bbox: [0.1, 0.2, 0.7, 0.8] }] });
    const { container } = render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    expect(await screen.findByText(/Source overview unavailable/)).toBeInTheDocument();
    expect(screen.getByAltText("Highlighted dog evidence")).toHaveAttribute("src", "legacy_crop.webp");
    expect(container.querySelector(".capture-evidence .capture-box")).toHaveStyle({ left: "10%", top: "20%", width: "60%" });
    expect(container.querySelector(".capture-source-image")).toBeNull();
  });
  it("keeps optional names per dog and cat without changing groups or confirming identity", async () => {
    vi.mocked(getCapture).mockResolvedValue({ ...detail, instances: [detail.instances[0], { ...detail.instances[1], species: "cat" }, detail.instances[2]] });
    vi.mocked(reviewCapture).mockResolvedValue({ capture_id: "c", sighting_ids: ["s1", "s2"], processing_state: "ready" });
    render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    const dog = await screen.findByRole("group", { name: "Dog 1" });
    const cat = screen.getByRole("group", { name: "Cat 1" });
    const dogName = within(dog).getByLabelText("Known name (optional)");
    const catName = within(cat).getByLabelText("Known name (optional)");
    expect(dogName).toHaveAttribute("maxlength", "80");
    expect(dogName).toHaveValue("");
    expect(screen.getByText(/Names don’t confirm identity or merge animals/)).toBeInTheDocument();
    await userEvent.type(dogName, "  Kaju  ");
    await userEvent.type(catName, "Kaju");
    await userEvent.click(screen.getByRole("button", { name: "Publish 2 animal sightings" }));
    expect(reviewCapture).toHaveBeenCalledWith("c", [{ instance_ids: ["a", "c"], known_name: "Kaju" }, { instance_ids: ["b"], known_name: "Kaju" }], 2);
  });
  it("does not copy a removed group's name onto a newly split animal", async () => {
    vi.mocked(getCapture).mockResolvedValue({ ...detail, groups: [{ instance_ids: ["a", "c"], known_name: "Kaju" }, { instance_ids: ["b"] }] });
    render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    await screen.findByRole("group", { name: "Dog 1" });
    await userEvent.selectOptions(screen.getByLabelText("Group for evidence 1"), "2");
    await userEvent.selectOptions(screen.getByLabelText("Group for evidence 3"), "2");
    await userEvent.selectOptions(screen.getByLabelText("Group for evidence 1"), "1");
    expect(within(screen.getByRole("group", { name: "Dog 1" })).getByLabelText("Known name (optional)")).toHaveValue("");
  });
  it("loads and clears a saved name without changing published associations", async () => {
    vi.mocked(getCapture).mockResolvedValue({ ...detail, processing_state: "ready", groups: [{ instance_ids: ["a", "c"], known_name: "Kaju" }, { instance_ids: ["b"] }] });
    vi.mocked(reviewCapture).mockResolvedValue({ capture_id: "c", sighting_ids: ["s1", "s2"], processing_state: "ready" });
    render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    const input = within(await screen.findByRole("group", { name: "Dog 1" })).getByLabelText("Known name (optional)");
    expect(input).toHaveValue("Kaju");
    await userEvent.clear(input);
    await userEvent.click(screen.getByRole("button", { name: "Save animal details" }));
    expect(reviewCapture).toHaveBeenCalledWith("c", [{ instance_ids: ["a", "c"], known_name: null }, { instance_ids: ["b"] }], 2);
  });
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
