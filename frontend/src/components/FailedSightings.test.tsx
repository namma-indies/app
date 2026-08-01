// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("../offline/queue", () => ({
  listFailed: vi.fn(),
  retryFailed: vi.fn(),
  discardFailed: vi.fn(),
  flush: vi.fn(),
}));

import { discardFailed, flush, listFailed, retryFailed } from "../offline/queue";
import FailedSightings from "./FailedSightings";

const ITEM = {
  id: 1,
  status: "failed" as const,
  photos: [],
  geo_source: "none" as const,
  captured_at: "2026-07-19T10:00:00Z",
};

// Not automatic here: Testing Library only registers its own afterEach when
// vitest runs with globals enabled, and this suite doesn't.
afterEach(cleanup);

beforeEach(() => {
  vi.mocked(listFailed).mockReset();
  vi.mocked(retryFailed).mockReset().mockResolvedValue(undefined);
  vi.mocked(discardFailed).mockReset().mockResolvedValue(undefined);
  vi.mocked(flush).mockReset().mockResolvedValue(undefined);
  // URL.createObjectURL doesn't exist in jsdom; the component calls it per item.
  Object.defineProperty(URL, "createObjectURL", { value: () => "blob:x", writable: true });
  Object.defineProperty(URL, "revokeObjectURL", { value: () => {}, writable: true });
});

describe("retrying a failed capture", () => {
  it("keeps showing the item while the retry is still in flight", async () => {
    // The queue reports it pending (so absent from listFailed) for as long as
    // the flush runs -- which is exactly when the sheet used to claim success.
    vi.mocked(listFailed).mockResolvedValueOnce([ITEM]).mockResolvedValue([]);
    let finishFlush!: () => void;
    vi.mocked(flush).mockReturnValue(new Promise<void>((r) => (finishFlush = r)));

    render(<FailedSightings onClose={() => {}} />);
    await screen.findByText("RETRY");
    await userEvent.click(screen.getByText("RETRY"));

    // Mid-flight: the sheet must not claim everything synced.
    expect(screen.queryByText("All caught up.")).not.toBeInTheDocument();

    finishFlush();
    await waitFor(() => expect(screen.getByText("All caught up.")).toBeInTheDocument());
  });

  it("still shows the item when the retry fails again", async () => {
    // Server rejects it once more, so it goes straight back to failed.
    vi.mocked(listFailed).mockResolvedValue([ITEM]);

    render(<FailedSightings onClose={() => {}} />);
    await screen.findByText("RETRY");
    await userEvent.click(screen.getByText("RETRY"));

    await waitFor(() => expect(vi.mocked(flush)).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByText("RETRY")).toBeInTheDocument());
    expect(screen.queryByText("All caught up.")).not.toBeInTheDocument();
  });

  it("does not strand the sheet if the flush rejects", async () => {
    vi.mocked(listFailed).mockResolvedValue([ITEM]);
    vi.mocked(flush).mockRejectedValue(new Error("offline"));

    render(<FailedSightings onClose={() => {}} />);
    await screen.findByText("RETRY");
    await userEvent.click(screen.getByText("RETRY"));

    // The button comes back rather than staying stuck in its spinner.
    await waitFor(() => expect(screen.getByText("RETRY")).toBeEnabled());
  });
});
