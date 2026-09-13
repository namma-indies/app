// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  getModerationQueue: vi.fn(),
  reviewSighting: vi.fn(),
  getFlaggedQueue: vi.fn(),
  ruleOnAnimal: vi.fn(),
}));

import {
  getFlaggedQueue,
  getModerationQueue,
  ruleOnAnimal,
  type FlaggedItem,
  type ModerationItem,
} from "../api";
import Moderation from "./Moderation";

afterEach(cleanup);

function flagged(over: Partial<FlaggedItem> = {}): FlaggedItem {
  return {
    sighting_id: "s1",
    captured_at: "2026-08-01T10:00:00Z",
    observer: "Priya",
    animal_confidence: 0.02,
    dog: 0.02,
    cat: 0.01,
    thumb_url: "https://example.test/a_thumb.webp",
    ...over,
  };
}

function reported(over: Partial<ModerationItem> = {}): ModerationItem {
  return {
    sighting_id: "r1",
    captured_at: "2026-08-01T10:00:00Z",
    review_status: "pending",
    observer: "Priya",
    report_count: 1,
    reasons: ["endangers_dog"],
    notes: [],
    thumb_url: "https://example.test/a_thumb.webp",
    ...over,
  };
}

/** The flagged queue lives behind a toggle in the same screen as the reported
 * one, so every test here has to switch to it first. */
async function openFlagged() {
  vi.mocked(getModerationQueue).mockResolvedValue({ items: [] });
  render(<Moderation onUnauthorized={() => {}} />);
  await userEvent.click(await screen.findByRole("button", { name: /NOT ANIMALS/ }));
}

describe("Moderation — the flagged queue", () => {
  it("says so plainly when nothing is flagged", async () => {
    vi.mocked(getFlaggedQueue).mockResolvedValue({ items: [] });
    await openFlagged();
    expect(await screen.findByText(/NOTHING FLAGGED/)).toBeInTheDocument();
  });

  it("shows dog and cat separately, not the max", async () => {
    // The question being answered is "is there a DOG in this". A bare 0.82
    // cannot answer it -- that might have been a cat.
    vi.mocked(getFlaggedQueue).mockResolvedValue({
      items: [flagged({ dog: 0.03, cat: 0.82, animal_confidence: 0.82 })],
    });
    await openFlagged();
    expect(await screen.findByText(/DOG 0\.03/)).toBeInTheDocument();
    expect(screen.getByText(/CAT 0\.82/)).toBeInTheDocument();
  });

  it("lists the least animal-like first, in the order the API gave them", async () => {
    // The order is the instrument: you walk it from the top until the photos
    // start being real dogs, and that is where the threshold goes. Re-sorting
    // client-side would break that, so assert the API order is preserved.
    vi.mocked(getFlaggedQueue).mockResolvedValue({
      items: [
        flagged({ sighting_id: "low", dog: 0.01, cat: 0.0, observer: "Least" }),
        flagged({ sighting_id: "mid", dog: 0.4, cat: 0.0, observer: "Middle" }),
      ],
    });
    await openFlagged();
    await screen.findByText(/Least/);
    const shown = screen.getAllByText(/logged by/).map((n) => n.textContent);
    expect(shown[0]).toMatch(/Least/);
    expect(shown[1]).toMatch(/Middle/);
  });

  it("keeps one the moderator says is real, and drops the card", async () => {
    // Dropped locally rather than refetched, matching the reported queue: the
    // list must not reorder under someone working down it.
    vi.mocked(getFlaggedQueue).mockResolvedValue({ items: [flagged()] });
    vi.mocked(ruleOnAnimal).mockResolvedValue(undefined);
    await openFlagged();

    await userEvent.click(await screen.findByRole("button", { name: /THERE IS AN ANIMAL/ }));
    expect(ruleOnAnimal).toHaveBeenCalledWith("s1", "animal");
    await waitFor(() => expect(screen.queryByText(/logged by Priya/)).toBeNull());
  });

  it("confirms one the detector was right about", async () => {
    vi.mocked(getFlaggedQueue).mockResolvedValue({ items: [flagged()] });
    vi.mocked(ruleOnAnimal).mockResolvedValue(undefined);
    await openFlagged();

    await userEvent.click(await screen.findByRole("button", { name: /^NO ANIMAL$/ }));
    expect(ruleOnAnimal).toHaveBeenCalledWith("s1", "no_animal");
    await waitFor(() => expect(screen.queryByText(/logged by Priya/)).toBeNull());
  });

  it("a failed flagged fetch does not strand the moderator away from the reported queue", async () => {
    // The two queues fetch independently. If the flagged one fails, the
    // toggle must stay on screen -- otherwise a moderator who was mid-way
    // through the (working) reported queue has no way back short of a
    // reload.
    vi.mocked(getModerationQueue).mockResolvedValue({ items: [reported()] });
    vi.mocked(getFlaggedQueue).mockRejectedValue(new Error("network down"));
    render(<Moderation onUnauthorized={() => {}} />);

    await screen.findByText(/logged by Priya/);

    await userEvent.click(await screen.findByRole("button", { name: /NOT ANIMALS/ }));
    expect(await screen.findByText(/COULDN'T LOAD THE QUEUE/)).toBeInTheDocument();
    // The reported card must not still be on screen once we're on the failed
    // flagged queue -- the two views are not stacked.
    expect(screen.queryByText(/logged by Priya/)).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "REPORTED" }));
    expect(await screen.findByText(/logged by Priya/)).toBeInTheDocument();
  });
});
