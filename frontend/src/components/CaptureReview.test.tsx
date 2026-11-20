// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import CaptureReview, { ANIMAL_COLORS, animalLetter, groupingError, NOT_AN_ANIMAL } from "./CaptureReview";
import { getCapture, reviewCapture, type CaptureDetail, type CaptureInstance } from "../captureApi";
vi.mock("../captureApi", () => ({ getCapture: vi.fn(), reviewCapture: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
const instance = (id: string, photo_id: string, track_id = id, timestamp_ms: number | null = null): CaptureInstance => ({
  id, photo_id, track_id, sighting_id: null, species: "dog", thumb_url: `${id}.webp`, timestamp_ms,
  source_thumb_url: `${photo_id}_thumb.webp`, source_width: 200, source_height: 400, source_bbox: [0.1, 0.2, 0.5, 0.6],
});
// Two dogs: A is in frames 1 and 2, B only in frame 1 beside A.
const detail: CaptureDetail = { capture_id: "c", processing_state: "needs_review", captured_at: "2026-09-15T12:00:00Z", note: "shared note", revision: 2, sighting_ids: [],
  groups: [{ instance_ids: ["a", "c"], species: "dog" }, { instance_ids: ["b"], species: "dog" }],
  animal_hints: [{ instance_ids: ["a", "c"], species: "dog", confident: true, confidence: 0.9, representative_instance_id: "c" },
    { instance_ids: ["b"], species: "dog", confident: true, confidence: 0.95, representative_instance_id: "b" }],
  instances: [instance("a", "frame1", "track1", 0), instance("b", "frame1", "track2", 0), instance("c", "frame2", "track1", 1500)] };
const open = (value: CaptureDetail = detail) => {
  vi.mocked(getCapture).mockResolvedValue(value);
  vi.mocked(reviewCapture).mockResolvedValue({ capture_id: "c", sighting_ids: ["s1", "s2"], processing_state: "ready" });
  return render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
};
const card = (name: string) => screen.getByRole("article", { name });
const colorOf = (element: Element) => (element as HTMLElement).style.getPropertyValue("--evidence-color");

describe("one card per animal", () => {
  it("shows each animal once, lettered and coloured, with their best photo as the face and their frames in time order", async () => {
    const { container } = open({ ...detail, instances: [detail.instances[2], detail.instances[0], detail.instances[1]] });
    const a = await screen.findByRole("article", { name: "Dog A" });
    const b = card("Dog B");
    expect(screen.queryByRole("region", { name: /Photo \d/ })).not.toBeInTheDocument();
    expect(within(a).getByAltText("Dog A, their clearest photo")).toHaveAttribute("src", "c.webp");
    expect(within(a).getByText("In 2 photos")).toBeInTheDocument();
    expect(colorOf(a)).toBe(ANIMAL_COLORS[0]);
    expect(colorOf(b)).toBe(ANIMAL_COLORS[1]);
    // The frames, oldest first, behind a summary that starts closed for a confident animal.
    const frames = within(a).getAllByRole("listitem");
    expect(frames.map((f) => f.getAttribute("aria-label"))).toEqual(["Box 1", "Box 3"]);
    expect(within(frames[1]).getByText(/1\.5s/)).toBeInTheDocument();
    expect(a.querySelector("details")).not.toHaveAttribute("open");
    // In frame 1, A's box and B's box are both drawn, each in its animal's colour; A's is the solid one.
    const boxes = frames[0].querySelectorAll(".capture-source-box");
    expect(boxes).toHaveLength(2);
    expect(colorOf(boxes[0])).toBe(ANIMAL_COLORS[0]);
    expect(colorOf(boxes[1])).toBe(ANIMAL_COLORS[1]);
    expect(boxes[0]).toHaveClass("is-selected");
    expect(boxes[0]).toHaveTextContent("A");
    expect(container.querySelectorAll(".animal-card")).toHaveLength(2);
  });
  it("publishes the pre-filled grouping in one tap", async () => {
    const onSaved = vi.fn();
    vi.mocked(getCapture).mockResolvedValue(detail);
    vi.mocked(reviewCapture).mockResolvedValue({ capture_id: "c", sighting_ids: ["s1", "s2"], processing_state: "ready" });
    render(<CaptureReview captureId="c" onClose={() => {}} onSaved={onSaved} onUnauthorized={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Looks right · publish 2 animals" }));
    expect(reviewCapture).toHaveBeenCalledWith("c", [{ instance_ids: ["a", "c"], species: "dog" }, { instance_ids: ["b"], species: "dog" }], 2, []);
    expect(onSaved).toHaveBeenCalledOnce();
  });
  it("marks an animal the grouping is unsure about and opens their photos", async () => {
    open({ ...detail, animal_hints: [{ ...detail.animal_hints![0], confident: false, reasons: ["close_alternative"] }, detail.animal_hints![1]] });
    const a = await screen.findByRole("article", { name: "Dog A" });
    expect(a).toHaveClass("is-unsure");
    expect(within(a).getByText(/not sure every photo here shows the same dog/)).toBeInTheDocument();
    expect(a.querySelector("details")).toHaveAttribute("open");
    expect(card("Dog B")).not.toHaveClass("is-unsure");
    expect(screen.getByText(/not sure about one of them/)).toBeInTheDocument();
  });
  it("says when a dog might be a cat, and the toggle sets the species they publish as", async () => {
    open({ ...detail, animal_hints: [detail.animal_hints![0], { ...detail.animal_hints![1], species_uncertain: true }] });
    const b = await screen.findByRole("article", { name: "Dog B" });
    expect(within(b).getByText("Dog B might be a cat. Tap Cat if so.")).toBeInTheDocument();
    const toggle = within(b).getByRole("radiogroup", { name: "Is B a dog or a cat?" });
    expect(within(toggle).getByRole("radio", { name: "Dog" })).toHaveAttribute("aria-checked", "true");
    await userEvent.click(within(toggle).getByRole("radio", { name: "Cat" }));
    const cat = card("Cat B");
    expect(within(cat).queryByText(/might be a/)).not.toBeInTheDocument();
    expect(within(cat).getByRole("group", { name: "Cat B" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Looks right · publish 2 animals" }));
    expect(reviewCapture).toHaveBeenCalledWith("c", [{ instance_ids: ["a", "c"], species: "dog" }, { instance_ids: ["b"], species: "cat" }], 2, []);
  });
  it("uses the server's species for an animal whose boxes carry mixed labels", async () => {
    open({ ...detail, groups: [{ instance_ids: ["a", "c"], species: "cat" }, detail.groups[1]],
      instances: [{ ...detail.instances[0], species: "cat" }, detail.instances[1], detail.instances[2]] });
    expect(await screen.findByRole("article", { name: "Cat A" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("animal-level actions", () => {
  it("merges two animals into one, and refuses when they share a photo", async () => {
    const apart = { ...detail, groups: [{ instance_ids: ["a"], species: "dog" as const }, { instance_ids: ["c"], species: "dog" as const }, { instance_ids: ["b"], species: "dog" as const }],
      animal_hints: undefined };
    open(apart);
    await screen.findByRole("article", { name: "Dog C" });
    // A (box a) and B (box c) are in different photos: one dog.
    await userEvent.selectOptions(screen.getByLabelText("Dog B is the same animal as"), "1");
    expect(screen.queryByRole("article", { name: "Dog B" })).not.toBeInTheDocument();
    expect(within(card("Dog A")).getByText("In 2 photos")).toBeInTheDocument();
    // C (box b) was beside A in photo 1: two different dogs.
    await userEvent.selectOptions(screen.getByLabelText("Dog C is the same animal as"), "1");
    expect(screen.getByRole("alert")).toHaveTextContent("Dog C and Dog A are both in photo 1, so they’re two different animals.");
    expect(card("Dog C")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Publish 2 animals" }));
    expect(reviewCapture).toHaveBeenCalledWith("c", [{ instance_ids: ["a", "c"], species: "dog" }, { instance_ids: ["b"], species: "dog" }], 2, []);
  });
  it("moves one box to another animal or a new one, and the box takes that animal's colour", async () => {
    open();
    await screen.findByRole("article", { name: "Dog A" });
    await userEvent.click(within(card("Dog A")).getByText("All 2 photos of them"));
    // Box 1 is in the same photo as B: B is not offered for it.
    expect(within(screen.getByLabelText("Which animal is box 1?")).getByRole("option", { name: "Dog B" })).toBeDisabled();
    await userEvent.selectOptions(screen.getByLabelText("Which animal is box 3?"), "2");
    const b = card("Dog B");
    expect(within(b).getByText("In 2 photos")).toBeInTheDocument();
    expect(colorOf(b.querySelector("#capture-box-c")!)).toBe(ANIMAL_COLORS[1]);
    await userEvent.selectOptions(screen.getByLabelText("Which animal is box 3?"), "new");
    const created = screen.getAllByRole("article").find((a) => a.getAttribute("aria-label")?.startsWith("Dog") && !["Dog A", "Dog B"].includes(a.getAttribute("aria-label")!))!;
    expect(created).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Publish 3 animals" })).toBeEnabled();
  });
  it("leaves a whole animal out as not an animal, and can put a box back", async () => {
    open();
    await screen.findByRole("article", { name: "Dog B" });
    await userEvent.click(screen.getByRole("button", { name: "Dog B isn’t an animal: leave them out" }));
    expect(screen.queryByRole("article", { name: "Dog B" })).not.toBeInTheDocument();
    const leftOut = screen.getByRole("region", { name: "Left out" });
    expect(within(leftOut).getByRole("listitem", { name: "Box 2" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Publish 1 animal" }));
    expect(reviewCapture).toHaveBeenCalledWith("c", [{ instance_ids: ["a", "c"], species: "dog" }], 2, ["b"]);
  });
  it("discards the upload only after confirming when no animal is left", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    open();
    await screen.findByRole("article", { name: "Dog A" });
    await userEvent.click(screen.getByRole("button", { name: "Dog A isn’t an animal: leave them out" }));
    await userEvent.click(screen.getByRole("button", { name: "Dog B isn’t an animal: leave them out" }));
    await userEvent.click(screen.getByRole("button", { name: "Discard this upload" }));
    expect(reviewCapture).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Discard this upload" }));
    expect(reviewCapture).toHaveBeenCalledWith("c", [], 2, ["a", "b", "c"]);
    confirm.mockRestore();
  });
  it("keeps details per animal and publishes them with the grouping", async () => {
    open();
    const a = await screen.findByRole("group", { name: "Dog A" });
    await userEvent.type(within(a).getByLabelText("Known name (optional)"), "  Kaju  ");
    await userEvent.click(within(screen.getByRole("group", { name: "Dog B" })).getByRole("radio", { name: "injured" }));
    expect(screen.getByText(/A name doesn’t decide which dog this is/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Looks right · publish 2 animals" }));
    expect(reviewCapture).toHaveBeenCalledWith("c", [{ instance_ids: ["a", "c"], species: "dog", known_name: "Kaju" },
      { instance_ids: ["b"], species: "dog", condition: "injured" }], 2, []);
  });
  it("rejects two boxes of one photo in one animal, and boxes without one", () => {
    expect(groupingError(detail.instances, { a: "1", b: "1", c: "2" })).toMatch(/same photo are two different animals/);
    expect(groupingError(detail.instances, { a: "1" })).toMatch(/Every box needs an animal/);
    expect(groupingError(detail.instances, { a: "1", b: NOT_AN_ANIMAL, c: "1" })).toBeNull();
    const mixed = [detail.instances[0], { ...detail.instances[2], species: "cat" }];
    expect(groupingError(mixed, { a: "1", c: "1" })).toBeNull();
  });
  it("letters animals A..Z, then AA", () => {
    expect(["1", "2", "26", "27", "28"].map(animalLetter)).toEqual(["A", "B", "Z", "AA", "AB"]);
  });
});

describe("published and locked", () => {
  it("locks grouping and species once published, keeping details editable", async () => {
    open({ ...detail, processing_state: "ready" });
    const a = await screen.findByRole("article", { name: "Dog A" });
    expect(screen.queryByText(/Looks right/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Which animal is box/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/is the same animal as/)).not.toBeInTheDocument();
    expect(within(a).getByRole("radio", { name: "Cat" })).toBeDisabled();
    expect(screen.getAllByRole("radio", { name: "unsure" })[0]).toBeEnabled();
    expect(screen.getByRole("button", { name: "Save animal details" })).toBeEnabled();
  });
  it.each(["processing", "failed", "no_animal"] as const)("does not publish evidence in state %s", async (processing_state) => {
    open({ ...detail, processing_state });
    expect(await screen.findByRole("button", { name: /publish 2 animals/i })).toBeDisabled();
    expect(reviewCapture).not.toHaveBeenCalled();
  });
  it("requires fresh evidence after a lost save acknowledgement before submitting again", async () => {
    vi.mocked(getCapture).mockResolvedValueOnce(detail).mockResolvedValue({ ...detail, revision: 3, processing_state: "ready" });
    vi.mocked(reviewCapture).mockRejectedValue(new TypeError("lost response"));
    render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Looks right · publish 2 animals" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Reload");
    expect(screen.getByRole("button", { name: "Looks right · publish 2 animals" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Reload" }));
    expect(await screen.findByRole("button", { name: "Save animal details" })).toBeEnabled();
    expect(reviewCapture).toHaveBeenCalledOnce();
  });
  it("preserves unsaved work when the parent changes its authorization callback", async () => {
    vi.mocked(getCapture).mockResolvedValue(detail);
    const props = { captureId: "c", onClose: vi.fn(), onSaved: vi.fn() };
    const { rerender } = render(<CaptureReview {...props} onUnauthorized={() => {}} />);
    await screen.findByRole("article", { name: "Dog A" });
    await userEvent.click(within(card("Dog B")).getByRole("radio", { name: "Cat" }));
    rerender(<CaptureReview {...props} onUnauthorized={() => {}} />);
    expect(getCapture).toHaveBeenCalledTimes(1);
    expect(card("Cat B")).toBeInTheDocument();
  });
});

describe("one-animal sheet", () => {
  const one: CaptureDetail = { ...detail, processing_state: "ready", groups: [{ instance_ids: ["a"], sex: "male", species: "dog" }], animal_hints: undefined,
    instances: [{ ...instance("a", "frame1"), thumb_url: "a-crop.webp", bbox: [0.05, 0.05, 0.95, 0.95] }] };
  const renderOne = (extra: Partial<CaptureDetail> = {}) => open({ ...one, ...extra });
  it("is just the details: no card, photo strip, box picker or species toggle", async () => {
    const { container } = renderOne();
    await screen.findByRole("group", { name: "Dog A" });
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Which animal is box/)).not.toBeInTheDocument();
    expect(container.querySelector(".capture-source-box")).toBeNull();
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
  it("faces the sheet with the animal's best view when the server names one", async () => {
    renderOne({ groups: [{ instance_ids: ["a", "b"], species: "dog" }], instances: [one.instances[0], { ...one.instances[0], id: "b", photo_id: "frame2", thumb_url: "b-crop.webp" }],
      animal_hints: [{ instance_ids: ["a", "b"], species: "dog", representative_instance_id: "b" }] });
    const thumb = await screen.findByRole("button", { name: "Show the whole photo" });
    expect(within(thumb).getByRole("img")).toHaveAttribute("src", "b-crop.webp");
  });
  it("keeps keyboard focus inside the photo dialog and hands it back to the thumbnail on close", async () => {
    const user = userEvent.setup();
    renderOne();
    const thumb = await screen.findByRole("button", { name: "Show the whole photo" });
    thumb.focus();
    await user.keyboard("{Enter}");
    const close = screen.getByRole("button", { name: "Close photo" });
    expect(close).toHaveFocus();
    const viewport = screen.getByRole("region", { name: /Photos of this animal/ });
    await user.tab({ shift: true });
    expect(viewport).toHaveFocus();
    await user.tab();
    expect(close).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(thumb).toHaveFocus();
  });
  it("sets a chip on tap, clears it when the chosen one is tapped again, and saves the result", async () => {
    const user = userEvent.setup();
    renderOne();
    const sex = await screen.findByRole("radiogroup", { name: "Sex" });
    const male = within(sex).getByRole("radio", { name: "♂ male" });
    expect(male).toBeChecked();
    await user.click(within(sex).getByRole("radio", { name: "♀ female" }));
    await user.click(within(sex).getByRole("radio", { name: "♀ female" }));
    expect(within(sex).queryByRole("radio", { checked: true })).not.toBeInTheDocument();
    await user.click(within(screen.getByRole("radiogroup", { name: "Ear notch" })).getByRole("radio", { name: "left" }));
    await user.click(screen.getByRole("button", { name: "Save animal details" }));
    expect(reviewCapture).toHaveBeenCalledWith("c", [{ instance_ids: ["a"], sex: undefined, ear_notch: "left", species: "dog" }], 2, []);
  });
  it("loads and clears a saved name", async () => {
    renderOne({ groups: [{ instance_ids: ["a"], known_name: "Kaju", species: "dog" }] });
    const input = await screen.findByLabelText("Known name (optional)");
    expect(input).toHaveValue("Kaju");
    await userEvent.clear(input);
    await userEvent.click(screen.getByRole("button", { name: "Save animal details" }));
    expect(reviewCapture).toHaveBeenCalledWith("c", [{ instance_ids: ["a"], known_name: null, species: "dog" }], 2, []);
  });
});

describe("loading and species wording", () => {
  it("shows a neutral loading state, with no grouping intro, until the capture is known", async () => {
    let resolve!: (value: CaptureDetail) => void;
    vi.mocked(getCapture).mockReturnValue(new Promise((r) => { resolve = r; }));
    render(<CaptureReview captureId="c" onClose={() => {}} onSaved={() => {}} onUnauthorized={() => {}} />);
    expect(screen.getByText("Loading your photos…")).toBeInTheDocument();
    expect(screen.queryByText(/We sorted the photos/)).not.toBeInTheDocument();
    expect(screen.getByRole("dialog")).toHaveAccessibleName("Loading…");
    resolve({ ...detail, processing_state: "ready", groups: [{ instance_ids: ["a"] }], instances: [detail.instances[0]] });
    await screen.findByRole("button", { name: "Save animal details" });
    expect(screen.queryByText(/We sorted the photos/)).not.toBeInTheDocument();
  });
  it("explains the check once a capture that needs it has loaded", async () => {
    open();
    expect(await screen.findByText(/We sorted the photos into the animals we think we saw/)).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toHaveAccessibleName("Check the animals");
  });
  it.each(["cat", "dog"] as const)("names the species in the name note: %s", async (species) => {
    open({ ...detail, processing_state: "ready", groups: [{ instance_ids: ["a"], species }], animal_hints: undefined, instances: [{ ...detail.instances[0], species }] });
    expect(await screen.findByText(`A name doesn’t decide which ${species} this is.`)).toBeInTheDocument();
  });
  it("says 'animal' when the upload has a dog and a cat", async () => {
    open({ ...detail, groups: [detail.groups[0], { instance_ids: ["b"], species: "cat" }] });
    expect(await screen.findByText("A name doesn’t decide which animal this is.")).toBeInTheDocument();
  });
  it("falls back to the boxes' own labels for a server that sends no species", async () => {
    open({ ...detail, groups: [{ instance_ids: ["a", "c"] }, { instance_ids: ["b"] }], animal_hints: undefined,
      instances: [detail.instances[0], { ...detail.instances[1], species: "cat" }, detail.instances[2]] });
    expect(await screen.findByRole("article", { name: "Cat B" })).toBeInTheDocument();
    await waitFor(() => expect(card("Dog A")).toBeInTheDocument());
  });
});
