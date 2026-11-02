// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { getAccount, requestAccountDeletion, type Account } from "../api";
import DeleteAccount from "./DeleteAccount";
vi.mock("../api", async (original) => ({ ...await original<typeof import("../api")>(),
  getAccount: vi.fn(), requestAccountDeletion: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

const account: Account = { status: "active", purge_after: null, can_cancel: false, has_email: true, grace_days: 7,
  counts: { uploads: 4, sightings: 5, voice_notes: 2 } };

describe("Delete account (#179)", () => {
  it("only deletes once \"delete\" is typed, then hands back the schedule", async () => {
    vi.mocked(getAccount).mockResolvedValue(account);
    const pending = { ...account, status: "pending_deletion" as const, purge_after: "2026-11-09T10:00:00Z", can_cancel: true };
    vi.mocked(requestAccountDeletion).mockResolvedValue(pending);
    const scheduled = vi.fn();
    render(<DeleteAccount onClose={() => {}} onScheduled={scheduled} onUnauthorized={() => {}} />);
    expect(await screen.findByText(/5 sightings and 2 added voice notes/)).toBeInTheDocument();
    expect(screen.getByText(/deleted in 7 days/)).toBeInTheDocument();
    const go = screen.getByRole("button", { name: "Delete my account" });
    expect(go).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/to confirm/), "Delete");
    await userEvent.click(go);
    expect(requestAccountDeletion).toHaveBeenCalled();
    expect(scheduled).toHaveBeenCalledWith(pending);
  });
});
