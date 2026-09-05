// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  getModerationQueue: vi.fn(),
  reviewSighting: vi.fn(),
}));

import { getModerationQueue, reviewSighting, type ModerationItem } from "../api";
import Moderation from "./Moderation";

afterEach(cleanup);

function item(over: Partial<ModerationItem> = {}): ModerationItem {
  return {
    sighting_id: "s1",
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

describe("Moderation", () => {
  it("says so plainly when nothing is reported", async () => {
    vi.mocked(getModerationQueue).mockResolvedValue({ items: [] });
    render(<Moderation onUnauthorized={() => {}} />);
    expect(await screen.findByText(/NOTHING REPORTED/)).toBeInTheDocument();
  });

  it("shows why it was reported, not just that it was", async () => {
    // A moderator deciding whether a photo endangers an animal needs the
    // reason: the same picture can be fine or not depending on what someone
    // recognised in it.
    vi.mocked(getModerationQueue).mockResolvedValue({
      items: [item({ notes: ["shows the gate she sleeps behind"], report_count: 3 })],
    });
    render(<Moderation onUnauthorized={() => {}} />);

    expect(await screen.findByText(/PUTS THE DOG AT RISK/)).toBeInTheDocument();
    expect(screen.getByText(/shows the gate she sleeps behind/)).toBeInTheDocument();
    expect(screen.getByText(/3 REPORTS/)).toBeInTheDocument();
  });

  it("flags a sighting reported again after a moderator cleared it", async () => {
    // Otherwise a fresh concern about already-reviewed content looks identical
    // to a first report, and the moderator re-decides without knowing they are.
    vi.mocked(getModerationQueue).mockResolvedValue({
      items: [item({ review_status: "valid" })],
    });
    render(<Moderation onUnauthorized={() => {}} />);
    expect(await screen.findByText(/REPORTED AGAIN AFTER REVIEW/)).toBeInTheDocument();
  });

  it("hiding sends the verdict and drops the row", async () => {
    vi.mocked(getModerationQueue).mockResolvedValue({ items: [item()] });
    vi.mocked(reviewSighting).mockResolvedValue(undefined);
    render(<Moderation onUnauthorized={() => {}} />);

    await userEvent.click(await screen.findByRole("button", { name: "HIDE IT" }));

    expect(reviewSighting).toHaveBeenCalledWith("s1", "rejected");
    await waitFor(() => expect(screen.queryByText("HIDE IT")).not.toBeInTheDocument());
  });

  it("keeping it sends the opposite verdict", async () => {
    vi.mocked(getModerationQueue).mockResolvedValue({ items: [item()] });
    vi.mocked(reviewSighting).mockResolvedValue(undefined);
    render(<Moderation onUnauthorized={() => {}} />);

    await userEvent.click(await screen.findByRole("button", { name: "KEEP IT" }));

    expect(reviewSighting).toHaveBeenCalledWith("s1", "valid");
  });

  it("a note written by a reporter is rendered as text, never markup", async () => {
    vi.mocked(getModerationQueue).mockResolvedValue({
      items: [item({ notes: ['<img src=x onerror="alert(1)">'] })],
    });
    const { container } = render(<Moderation onUnauthorized={() => {}} />);

    await screen.findByText(/<img src=x/);
    expect(container.querySelector("img[onerror]")).toBeNull();
  });
});
