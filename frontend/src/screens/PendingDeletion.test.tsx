// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { cancelAccountDeletion, HttpError, type Account } from "../api";
import PendingDeletion from "./PendingDeletion";
vi.mock("../api", async (original) => ({ ...await original<typeof import("../api")>(), cancelAccountDeletion: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

const account = (over: Partial<Account> = {}): Account => ({ status: "pending_deletion", purge_after: "2026-11-09T10:00:00Z",
  can_cancel: true, has_email: true, grace_days: 7, counts: { uploads: 2, sightings: 3, voice_notes: 0 }, ...over });

describe("an account scheduled for deletion (#179)", () => {
  it("says when it goes and keeps it on request", async () => {
    vi.mocked(cancelAccountDeletion).mockResolvedValue(account({ status: "active", purge_after: null }));
    const kept = vi.fn();
    render(<PendingDeletion account={account()} onKept={kept} onSignOut={() => {}} onUnauthorized={() => {}} />);
    expect(screen.getByText(/It will be deleted on/)).toHaveTextContent("2026");
    await userEvent.click(screen.getByRole("button", { name: "Keep my account" }));
    expect(cancelAccountDeletion).toHaveBeenCalled();
    expect(kept).toHaveBeenCalled();
  });

  it("offers no way back once the purge has started", () => {
    render(<PendingDeletion account={account({ can_cancel: false })} onKept={() => {}} onSignOut={() => {}} onUnauthorized={() => {}} />);
    expect(screen.queryByRole("button", { name: "Keep my account" })).not.toBeInTheDocument();
    expect(screen.getByText(/can’t be undone/)).toBeInTheDocument();
  });

  it("says so when keeping it fails", async () => {
    vi.mocked(cancelAccountDeletion).mockRejectedValue(new HttpError(409));
    render(<PendingDeletion account={account()} onKept={() => {}} onSignOut={() => {}} onUnauthorized={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Keep my account" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });
});
