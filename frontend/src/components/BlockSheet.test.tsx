// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { blockSighting, getBlocks, HttpError, unblock } from "../api";
import BlockSheet from "./BlockSheet";
import BlockedPeople from "./BlockedPeople";
import { popupHtml } from "./DogMap";
vi.mock("../api", async (original) => ({ ...await original<typeof import("../api")>(),
  blockSighting: vi.fn(), getBlocks: vi.fn(), unblock: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("blocking (#180)", () => {
  it("blocks, and reports too only when asked", async () => {
    vi.mocked(blockSighting).mockResolvedValue();
    const blocked = vi.fn();
    render(<BlockSheet sightingId="s1" name="Ravi" onClose={() => {}} onBlocked={blocked} onUnauthorized={() => {}} />);
    expect(screen.getByText(/They aren’t told/)).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Also report this sighting" })).not.toBeChecked();
    await userEvent.click(screen.getByRole("button", { name: "BLOCK" }));
    expect(blockSighting).toHaveBeenCalledWith("s1");
    expect(blocked).toHaveBeenCalledWith(false);
  });

  it("passes on a wish to report as well", async () => {
    vi.mocked(blockSighting).mockResolvedValue();
    const blocked = vi.fn();
    render(<BlockSheet sightingId="s1" onClose={() => {}} onBlocked={blocked} onUnauthorized={() => {}} />);
    await userEvent.click(screen.getByRole("checkbox", { name: "Also report this sighting" }));
    await userEvent.click(screen.getByRole("button", { name: "BLOCK" }));
    expect(blocked).toHaveBeenCalledWith(true);
  });

  it("says so when the block fails", async () => {
    vi.mocked(blockSighting).mockRejectedValue(new HttpError(429));
    render(<BlockSheet sightingId="s1" onClose={() => {}} onBlocked={() => {}} onUnauthorized={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "BLOCK" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it("lists who you blocked, and unblocks", async () => {
    vi.mocked(getBlocks).mockResolvedValue({ blocks: [{ id: "b1", name: "Ravi", created_at: "2026-11-01T00:00:00Z" }] });
    vi.mocked(unblock).mockResolvedValue();
    const changed = vi.fn();
    render(<BlockedPeople onClose={() => {}} onChanged={changed} onUnauthorized={() => {}} />);
    expect(await screen.findByText("Ravi")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Unblock" }));
    expect(unblock).toHaveBeenCalledWith("b1");
    expect(changed).toHaveBeenCalled();
    expect(screen.getByText("You haven’t blocked anyone.")).toBeInTheDocument();
  });

  it("offers block beside report on someone else's pin only", () => {
    expect(popupHtml({ id: "s1", observer: "Ravi", time: "today" })).toContain('data-block="s1"');
    expect(popupHtml({ id: "s1", observer: "", time: "today" })).not.toContain("data-block");
  });
});
