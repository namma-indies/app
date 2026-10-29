// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import CaptureReview, { groupingError, NOT_AN_ANIMAL } from "./CaptureReview";
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
    const first = await screen.findByRole("region", { name: "Photo 1" });
    const second = screen.getByRole("region", { name: "Photo 2" });
    expect(within(first).getAllByRole("article")).toHaveLength(2);
    expect(within(second).getAllByRole("article")).toHaveLength(1);
    expect(within(second).getByRole("heading")).toHaveTextContent("1.5s");
    const photo = within(first).getByAltText("Whole photo 1");
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
    const firstButton = within(first).getByRole("button", { name: /Box 1/ });
    const overlappingButton = within(first).getByRole("button", { name: /Box 2/ });
    firstButton.focus();
    await user.tab();
    expect(overlappingButton).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(overlappingButton).toHaveAttribute("aria-pressed", "true");
    expect(firstButton).toHaveAttribute("aria-pressed", "false");
    expect(boxes[1]).toHaveClass("is-selected");
    expect(boxes[0]).not.toHaveClass("is-selected");
    expect(within(first).getByRole("article", { name: "Box 2" })).toHaveClass("is-selected");
    expect(screen.getByRole("group", { name: "Dog 2" })).toHaveClass("capture-entry-selected");
    await user.pointer([{ keys: "[TouchA>]", target: firstButton }, { keys: "[/TouchA]" }]);
    expect(firstButton).toHaveAttribute("aria-pressed", "true");
    expect(boxes[0]).toHaveClass("is-selected");
    overlappingButton.focus();
    await user.keyboard(" ");
    expect(overlappingButton).toHaveAttribute("aria-pressed", "true");
    expect(boxes[1]).toHaveClass("is-selected");
    expect(reviewCapture).not.toHaveBeenCalled();
    expect(screen.getByText(/numbers only tell boxes apart/)).toBeInTheDocument();
  });
  it("numbers groups separately by species across boxes, evidence, options and details", async () => {
    const instances = [detail.instances[0], { ...instance("cat", "cat-frame"), species: "cat" }, detail.instances[1], detail.instances[2]].map((item) => ({
      ...item, source_thumb_url: `${item.photo_id}_thumb.webp`, source_bbox: [0.1, 0.2, 0.5, 0.6] as [number, number, number, number],
    }));
    vi.mocked(getCapture).mockResolvedValue({ ...detail, instances, groups: [{ instance_ids: ["a", "c"] }, { instance_ids: ["cat"] }, { instance_ids: ["b"] }] });
    const { container } = render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    await screen.findByRole("group", { name: "Cat 1" });
    for (const [evidence, label] of [[1, "Dog 1"], [2, "Cat 1"], [3, "Dog 2"], [4, "Dog 1"]] as const) {
      const card = screen.getByRole("article", { name: `Box ${evidence}` });
      expect(within(card).getByRole("button")).toHaveTextContent(`Box ${evidence} · ${label}`);
      expect(within(card).getByRole("combobox")).toHaveDisplayValue(label);
      expect(screen.getByRole("group", { name: label })).toBeInTheDocument();
      expect(container.querySelector(`#capture-box-${instances[evidence - 1].id}`)).toHaveTextContent(`${label} · Box ${evidence}`);
    }
    const dogSelect = screen.getByLabelText("Which animal is box 1?");
    const catSelect = screen.getByLabelText("Which animal is box 2?");
    expect(within(dogSelect).queryByRole("option", { name: "Cat 1" })).not.toBeInTheDocument();
    expect(within(catSelect).queryByRole("option", { name: /Dog/ })).not.toBeInTheDocument();
    expect(within(dogSelect).getByRole("option", { name: "A new dog" })).toBeInTheDocument();
    expect(within(catSelect).getByRole("option", { name: "A new cat" })).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Which animal is box 4?"), "4");
    expect(screen.getByRole("group", { name: "Dog 3" })).toHaveTextContent("Box 4");
    expect(container.querySelector("#capture-box-c")).toHaveTextContent("Dog 3 · Box 4");
    await userEvent.selectOptions(screen.getByLabelText("Which animal is box 4?"), "3");
    expect(screen.queryByRole("group", { name: "Dog 3" })).not.toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Dog 2" })).toHaveTextContent("Box 3, 4");
    expect(container.querySelector("#capture-box-c")).toHaveTextContent("Dog 2 · Box 4");
    expect(screen.getByLabelText("Which animal is box 2?")).toHaveDisplayValue("Cat 1");
  });
  it("marks mixed-species groups invalid without labeling them as a dog or cat", async () => {
    const instances = [instance("a", "dog-frame"), { ...instance("cat", "cat-frame"), species: "cat" }, instance("b", "other-dog-frame")];
    vi.mocked(getCapture).mockResolvedValue({ ...detail, instances, groups: [{ instance_ids: ["a", "cat"] }, { instance_ids: ["b"] }] });
    render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    const mixed = "Mixed group 1 (needs splitting)";
    expect(await screen.findByRole("group", { name: mixed })).toHaveTextContent("Box 1, 2");
    expect(screen.getByRole("alert")).toHaveTextContent("Different species");
    expect(screen.getByRole("button", { name: "Publish 2 animals" })).toBeDisabled();
    expect(screen.getByLabelText("Which animal is box 1?")).toHaveDisplayValue(mixed);
    expect(within(screen.getByLabelText("Which animal is box 1?")).getByRole("option", { name: mixed })).toBeDisabled();
    expect(within(screen.getByLabelText("Which animal is box 3?")).queryByRole("option", { name: mixed })).not.toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Which animal is box 2?"), "3");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Cat 1" })).toHaveTextContent("Box 2");
    expect(screen.getByLabelText("Which animal is box 1?")).toHaveDisplayValue("Dog 1");
    expect(screen.getByLabelText("Which animal is box 3?")).toHaveDisplayValue("Dog 2");
    expect(screen.getByRole("button", { name: "Publish 3 animals" })).toBeEnabled();
  });
  it("keeps legacy crop evidence and crop-relative boxes when source dimensions are absent", async () => {
    vi.mocked(getCapture).mockResolvedValue({ ...detail, instances: [{ ...detail.instances[0], source_url: "legacy_crop.webp", bbox: [0.1, 0.2, 0.7, 0.8] }] });
    const { container } = render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    expect(await screen.findByText(/Couldn’t show the whole photo/)).toBeInTheDocument();
    expect(screen.getByAltText("Box showing a dog")).toHaveAttribute("src", "legacy_crop.webp");
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
    expect(screen.getByText(/A name doesn’t decide which dog this is/)).toBeInTheDocument();
    await userEvent.type(dogName, "  Kaju  ");
    await userEvent.type(catName, "Kaju");
    await userEvent.click(screen.getByRole("button", { name: "Publish 2 animals" }));
    expect(reviewCapture).toHaveBeenCalledWith("c", [{ instance_ids: ["a", "c"], known_name: "Kaju" }, { instance_ids: ["b"], known_name: "Kaju" }], 2, []);
  });
  it("does not copy a removed group's name onto a newly split animal", async () => {
    vi.mocked(getCapture).mockResolvedValue({ ...detail, groups: [{ instance_ids: ["a", "c"], known_name: "Kaju" }, { instance_ids: ["b"] }] });
    render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    await screen.findByRole("group", { name: "Dog 1" });
    await userEvent.selectOptions(screen.getByLabelText("Which animal is box 1?"), "2");
    await userEvent.selectOptions(screen.getByLabelText("Which animal is box 3?"), "2");
    await userEvent.selectOptions(screen.getByLabelText("Which animal is box 1?"), "1");
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
    expect(reviewCapture).toHaveBeenCalledWith("c", [{ instance_ids: ["a", "c"], known_name: null }, { instance_ids: ["b"] }], 2, []);
  });
  it("rejects co-visible merges and incomplete assignments", () => {
    expect(groupingError(detail.instances, { a: "1", b: "1", c: "2" })).toMatch(/same photo are two different animals/);
    expect(groupingError(detail.instances, { a: "1" })).toMatch(/Every box needs an animal/);
    expect(groupingError(detail.instances, { a: "1", b: "2", c: "1" })).toBeNull();
  });
  it("lets a contributor split fragments, join them to another animal, and publish separate details", async () => {
    vi.mocked(getCapture).mockResolvedValue(detail);
    vi.mocked(reviewCapture).mockResolvedValue({ capture_id: "c", sighting_ids: ["s1", "s2"], processing_state: "ready" });
    const onSaved = vi.fn();
    render(<CaptureReview captureId="c" onClose={() => {}} onSaved={onSaved} onUnauthorized={() => {}} />);
    await screen.findByText("Details for each animal");
    await userEvent.selectOptions(screen.getByLabelText("Which animal is box 3?"), "3");
    expect(screen.getByRole("button", { name: "Publish 3 animals" })).toBeEnabled();
    await userEvent.selectOptions(screen.getByLabelText("Which animal is box 3?"), "2");
    await userEvent.click(within(screen.getAllByRole("radiogroup", { name: "Sex" })[0]).getByRole("radio", { name: "♀ female" }));
    await userEvent.click(within(screen.getAllByRole("radiogroup", { name: "Condition" })[1]).getByRole("radio", { name: "injured" }));
    await userEvent.click(screen.getByRole("button", { name: "Publish 2 animals" }));
    expect(reviewCapture).toHaveBeenCalledWith("c", [{ instance_ids: ["a"], sex: "female" }, { instance_ids: ["b", "c"], condition: "injured" }], 2, []);
    expect(onSaved).toHaveBeenCalledOnce();
  });
  it("leaves a doubled box out instead of publishing it as a second animal", async () => {
    // Two boxes on one dog in one photo can never share an animal (#105).
    expect(groupingError(detail.instances, { a: "1", b: NOT_AN_ANIMAL, c: "1" })).toBeNull();
    vi.mocked(getCapture).mockResolvedValue(detail);
    vi.mocked(reviewCapture).mockResolvedValue({ capture_id: "c", sighting_ids: ["s1"], processing_state: "ready" });
    render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    await screen.findByText("Details for each animal");
    await userEvent.selectOptions(screen.getByLabelText("Which animal is box 2?"), NOT_AN_ANIMAL);
    expect(screen.getByRole("article", { name: "Box 2" })).toHaveClass("is-excluded");
    await userEvent.click(screen.getByRole("button", { name: "Publish 1 animal" }));
    expect(reviewCapture).toHaveBeenCalledWith("c", [{ instance_ids: ["a", "c"] }], 2, ["b"]);
  });
  it("discards the upload only after confirming when no box is an animal", async () => {
    vi.mocked(getCapture).mockResolvedValue(detail);
    vi.mocked(reviewCapture).mockResolvedValue({ capture_id: "c", sighting_ids: [], processing_state: "no_animal" });
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    await screen.findByText("Details for each animal");
    for (const box of [1, 2, 3]) await userEvent.selectOptions(screen.getByLabelText(`Which animal is box ${box}?`), NOT_AN_ANIMAL);
    await userEvent.click(screen.getByRole("button", { name: "Discard this upload" }));
    expect(reviewCapture).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Discard this upload" }));
    expect(reviewCapture).toHaveBeenCalledWith("c", [], 2, ["a", "b", "c"]);
    confirm.mockRestore();
  });
  it("shows a saved exclusion as not an animal", async () => {
    vi.mocked(getCapture).mockResolvedValue({ ...detail, processing_state: "ready", groups: [{ instance_ids: ["a", "c"] }], excluded_instance_ids: ["b"] });
    render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    // One animal left, so the sheet is just its details: no photo blocks and no locked picker.
    expect(await screen.findByRole("button", { name: "Save animal details" })).toBeEnabled();
    expect(screen.queryByLabelText("Which animal is box 2?")).not.toBeInTheDocument();
  });
  it("shows a ready upload with boxes still grouped in two animals with the photos and a locked picker", async () => {
    vi.mocked(getCapture).mockResolvedValue({ ...detail, processing_state: "ready" });
    render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    await screen.findByText("Details for each animal");
    expect(screen.getByLabelText("Which animal is box 2?")).toBeDisabled();
  });
  it("blocks publication of co-visible animals assigned to the same group", async () => {
    vi.mocked(getCapture).mockResolvedValue(detail);
    render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    await screen.findByText("Details for each animal");
    await userEvent.selectOptions(screen.getByLabelText("Which animal is box 2?"), "1");
    expect(screen.getByRole("alert")).toHaveTextContent("same photo are two different animals");
    expect(screen.getByRole("button", { name: "Publish 1 animal" })).toBeDisabled();
    expect(reviewCapture).not.toHaveBeenCalled();
  });
  it("preserves unsaved work when the parent changes its authorization callback", async () => {
    vi.mocked(getCapture).mockResolvedValue(detail);
    const props = { captureId: "c", onClose: vi.fn(), onSaved: vi.fn() };
    const { rerender } = render(<CaptureReview {...props} onUnauthorized={() => {}} />);
    await screen.findByText("Details for each animal");
    await userEvent.selectOptions(screen.getByLabelText("Which animal is box 3?"), "3");
    await userEvent.click(within(screen.getAllByRole("radiogroup", { name: "Sex" })[0]).getByRole("radio", { name: "♀ female" }));
    rerender(<CaptureReview {...props} onUnauthorized={() => {}} />);
    expect(getCapture).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText("Which animal is box 3?")).toHaveValue("3");
    expect(within(screen.getAllByRole("radiogroup", { name: "Sex" })[0]).getByRole("radio", { name: "♀ female" })).toBeChecked();
  });
  it("requires fresh evidence after a lost save acknowledgement before submitting again", async () => {
    vi.mocked(getCapture).mockResolvedValueOnce(detail).mockResolvedValue({ ...detail, revision: 3, processing_state: "ready" });
    vi.mocked(reviewCapture).mockRejectedValue(new TypeError("lost response"));
    render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Publish 2 animals" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Reload");
    expect(screen.getByRole("button", { name: "Publish 2 animals" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Reload" }));
    expect(await screen.findByRole("button", { name: "Save animal details" })).toBeEnabled();
    expect(screen.getByLabelText("Which animal is box 1?")).toBeDisabled();
    expect(reviewCapture).toHaveBeenCalledOnce();
  });
  it.each(["processing", "failed", "no_animal"] as const)("does not publish evidence in state %s", async (processing_state) => {
    vi.mocked(getCapture).mockResolvedValue({ ...detail, processing_state });
    render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    expect(await screen.findByRole("button", { name: "Publish 2 animals" })).toBeDisabled();
    expect(reviewCapture).not.toHaveBeenCalled();
  });
  it("locks published grouping while preserving editable per-animal details", async () => {
    vi.mocked(getCapture).mockResolvedValue({ ...detail, processing_state: "ready" });
    render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    await waitFor(() => expect(screen.getByLabelText("Which animal is box 1?")).toBeDisabled());
    expect(screen.getAllByRole("radio", { name: "unsure" })[0]).toBeEnabled();
    expect(screen.getByRole("button", { name: "Save animal details" })).toBeEnabled();
  });
});

describe("one-animal sheet", () => {
  const one: CaptureDetail = { ...detail, processing_state: "ready", groups: [{ instance_ids: ["a"], sex: "male" }], instances: [{
    ...instance("a", "frame1"), thumb_url: "a-crop.webp", source_thumb_url: "frame1_thumb.webp", source_width: 200, source_height: 400, source_bbox: [0.1, 0.2, 0.5, 0.6], bbox: [0.05, 0.05, 0.95, 0.95],
  }] };
  const renderOne = (extra: Partial<CaptureDetail> = {}) => {
    vi.mocked(getCapture).mockResolvedValue({ ...one, ...extra });
    return render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
  };
  it("is just the details: no photo block, box picker or box hint", async () => {
    const { container } = renderOne();
    await screen.findByRole("group", { name: "Dog 1" });
    expect(screen.queryByRole("region", { name: /Photo/ })).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Which animal is box/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Tap a box/)).not.toBeInTheDocument();
    expect(container.querySelector(".capture-source-box")).toBeNull();
    expect(screen.queryByText("Details for each animal")).not.toBeInTheDocument();
    // The name comes before the chips, and Save sits outside the scrolling body so it stays reachable.
    const name = screen.getByLabelText("Known name (optional)");
    expect(name.compareDocumentPosition(screen.getByRole("radiogroup", { name: "Sex" })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole("button", { name: "Save animal details" }).closest(".capture-review-body")).toBeNull();
  });
  it("shows a thumbnail of the animal's box that opens the whole photo with the box, and closes again", async () => {
    const user = userEvent.setup();
    const { container } = renderOne();
    const thumb = await screen.findByRole("button", { name: "Show the whole photo" });
    expect(within(thumb).getByRole("img")).toHaveAttribute("src", "a-crop.webp");
    await user.click(thumb);
    const box = screen.getByRole("dialog", { name: "Photo with the animal boxed" });
    expect(within(box).getByAltText("Whole photo 1")).toHaveAttribute("src", "frame1_thumb.webp");
    expect(container.querySelectorAll(".capture-lightbox .capture-source-box")).toHaveLength(1);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Photo with the animal boxed" })).not.toBeInTheDocument();
    await user.click(thumb);
    await user.click(screen.getByRole("button", { name: "Close photo" }));
    expect(screen.queryByRole("dialog", { name: "Photo with the animal boxed" })).not.toBeInTheDocument();
  });
  it("opens a multi-photo lightbox at the first photo: Close is outside the scrolling stack and focus doesn't scroll", async () => {
    const user = userEvent.setup();
    const frame = (id: string, photo: string) => ({ ...one.instances[0], id, photo_id: photo, source_thumb_url: `${photo}_thumb.webp` });
    renderOne({ groups: [{ instance_ids: ["a", "b"] }], instances: [frame("a", "frame1"), frame("b", "frame2")] });
    const focus = vi.spyOn(HTMLElement.prototype, "focus");
    await user.click(await screen.findByRole("button", { name: "Show the whole photo" }));
    const body = document.querySelector(".capture-lightbox-body")!;
    expect(body.querySelectorAll("img")).toHaveLength(2);
    const close = screen.getByRole("button", { name: "Close photo" });
    expect(body.contains(close)).toBe(false);
    expect(focus.mock.instances).toContain(close);
    expect(focus).toHaveBeenCalledWith({ preventScroll: true });
    focus.mockRestore();
  });
  it("keeps keyboard focus inside the photo dialog and hands it back to the thumbnail on close", async () => {
    const user = userEvent.setup();
    renderOne();
    const thumb = await screen.findByRole("button", { name: "Show the whole photo" });
    thumb.focus();
    await user.keyboard("{Enter}");
    const close = screen.getByRole("button", { name: "Close photo" });
    expect(close).toHaveFocus();
    // The photo viewport is in the cycle (so arrows/PageDown can scroll it); nothing behind the dialog is.
    const viewport = screen.getByRole("region", { name: /Photos of this animal/ });
    await user.tab({ shift: true });
    expect(viewport).toHaveFocus();
    await user.tab({ shift: true });
    expect(close).toHaveFocus();
    await user.tab();
    expect(viewport).toHaveFocus();
    await user.tab();
    expect(close).toHaveFocus();
    expect(reviewCapture).not.toHaveBeenCalled();
    await user.keyboard("{Escape}");
    expect(thumb).toHaveFocus();
  });
  it("sets a chip on tap, clears it when the chosen one is tapped again, and saves the result", async () => {
    const user = userEvent.setup();
    vi.mocked(reviewCapture).mockResolvedValue({ capture_id: "c", sighting_ids: [], processing_state: "ready" });
    renderOne();
    const sex = await screen.findByRole("radiogroup", { name: "Sex" });
    const male = within(sex).getByRole("radio", { name: "♂ male" });
    expect(male).toBeChecked();
    await user.click(within(sex).getByRole("radio", { name: "♀ female" }));
    expect(male).not.toBeChecked();
    await user.click(within(sex).getByRole("radio", { name: "♀ female" }));
    expect(within(sex).queryByRole("radio", { checked: true })).not.toBeInTheDocument();
    await user.click(within(screen.getByRole("radiogroup", { name: "Ear notch" })).getByRole("radio", { name: "left" }));
    await user.click(screen.getByRole("button", { name: "Save animal details" }));
    expect(reviewCapture).toHaveBeenCalledWith("c", [{ instance_ids: ["a"], sex: undefined, ear_notch: "left" }], 2, []);
  });
  it("is a radiogroup you can drive from the keyboard, with one tab stop", async () => {
    const user = userEvent.setup();
    renderOne();
    const ear = await screen.findByRole("radiogroup", { name: "Ear notch" });
    const radios = within(ear).getAllByRole("radio");
    expect(radios.map((r) => r.getAttribute("tabindex"))).toEqual(["0", "-1", "-1", "-1"]);
    radios[0].focus();
    await user.keyboard("{ArrowRight}");
    expect(radios[1]).toHaveFocus();
    expect(radios[1]).toBeChecked();
    await user.keyboard("{ArrowLeft}{ArrowLeft}");
    expect(radios[3]).toHaveFocus();
    expect(radios[3]).toBeChecked();
    await user.keyboard(" ");
    expect(radios[3]).not.toBeChecked();
  });
  it("keeps the photo, boxes and pinned Save in a two-animal sheet, with chips instead of selects", async () => {
    vi.mocked(getCapture).mockResolvedValue({ ...detail, processing_state: "ready" });
    const { container } = render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    await screen.findByText("Details for each animal");
    expect(screen.getAllByRole("radiogroup", { name: "Sex" })).toHaveLength(2);
    expect(container.querySelector("fieldset select")).toBeNull();
    expect(screen.getByRole("button", { name: "Save animal details" }).closest(".capture-review-body")).toBeNull();
  });
});
