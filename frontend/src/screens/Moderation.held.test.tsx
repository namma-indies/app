// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  getModerationQueue: vi.fn(),
  getHeldQueue: vi.fn(),
  getHeldFrames: vi.fn(),
  ruleOnUpload: vi.fn(),
}));

import { getHeldFrames, getHeldQueue, getModerationQueue, ruleOnUpload, type HeldItem } from "../api";
import Moderation from "./Moderation";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function held(over: Partial<HeldItem> = {}): HeldItem {
  return {
    capture_id: "c1",
    created_at: "2026-11-01T10:00:00Z",
    kind: "photo",
    reasons: ["person", "new_account"],
    observer: "Priya",
    observer_id: "o1",
    note: "by the gate",
    known_names: ["Rani"],
    frames: [
      { thumb_url: "https://example.test/1_thumb.webp", url: "https://example.test/1.webp", flagged: true },
      { thumb_url: "https://example.test/2_thumb.webp", url: "https://example.test/2.webp", flagged: false },
    ],
    frame_count: 2,
    ...over,
  };
}

async function openHeld(items: HeldItem[]) {
  vi.mocked(getModerationQueue).mockResolvedValue({ items: [] });
  vi.mocked(getHeldQueue).mockResolvedValue({ items });
  render(<Moderation onUnauthorized={() => {}} />);
  await userEvent.click(await screen.findByRole("button", { name: "HELD" }));
}

describe("held uploads (#181)", () => {
  it("shows why it was held, every whole frame, and the text that would go public", async () => {
    await openHeld([held()]);
    expect(await screen.findByText("A PERSON FILLS THE FRAME")).toBeInTheDocument();
    expect(screen.getByText("NEW ACCOUNT")).toBeInTheDocument();
    expect(screen.getAllByRole("img")).toHaveLength(2);
    // Each frame opens at full size, for what the preview is too small to show.
    expect(screen.getByAltText("held upload, photo 1, flagged").closest("a")).toHaveAttribute(
      "href", "https://example.test/1.webp");
    expect(screen.getByText("by the gate")).toBeInTheDocument();
    expect(screen.getByText("name: Rani")).toBeInTheDocument();
  });

  it("approving sends the verdict and drops the upload from the list", async () => {
    vi.mocked(ruleOnUpload).mockResolvedValue();
    await openHeld([held(), held({ capture_id: "c2", observer: "Arun" })]);
    const show = await screen.findAllByRole("button", { name: "SHOW IT" });
    await userEvent.click(show[0]);
    expect(ruleOnUpload).toHaveBeenCalledWith("c1", "approve");
    await waitFor(() => expect(screen.getAllByRole("button", { name: "SHOW IT" })).toHaveLength(1));
  });

  it("rejecting keeps it hidden", async () => {
    vi.mocked(ruleOnUpload).mockResolvedValue();
    await openHeld([held()]);
    await userEvent.click(await screen.findByRole("button", { name: "KEEP IT HIDDEN" }));
    expect(ruleOnUpload).toHaveBeenCalledWith("c1", "reject");
    expect(await screen.findByText(/NOTHING HELD/)).toBeInTheDocument();
  });

  it("a longer upload cannot be shown until every photo has been opened", async () => {
    vi.mocked(ruleOnUpload).mockResolvedValue();
    vi.mocked(getHeldFrames).mockResolvedValue({
      frames: Array.from({ length: 6 }, (_, i) => ({
        thumb_url: `https://example.test/${i}_thumb.webp`, url: `https://example.test/${i}.webp`, flagged: i === 0,
      })),
    });
    await openHeld([held({ frame_count: 6 })]);
    expect(await screen.findByRole("button", { name: "SHOW IT" })).toBeDisabled();
    expect(screen.getByAltText("held upload, photo 1, flagged")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /SEE ALL 6 PHOTOS · 4 NOT SHOWN YET/ }));
    expect(getHeldFrames).toHaveBeenCalledWith("c1");
    await waitFor(() => expect(screen.getAllByRole("img")).toHaveLength(6));
    expect(screen.getByRole("button", { name: "SHOW IT" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: /SEE ALL/ })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "SHOW IT" }));
    expect(ruleOnUpload).toHaveBeenCalledWith("c1", "approve");
  });

  it("an upload screening never finished says so", async () => {
    await openHeld([held({ reasons: ["not_screened"] })]);
    expect(await screen.findByText("NOT CHECKED YET")).toBeInTheDocument();
  });
});
