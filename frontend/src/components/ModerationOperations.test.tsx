// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ModerationAlerts, ModerationAudit } from "./ModerationOperations";
import { getModerationAlertStatus, getModerationAudit } from "../api";

vi.mock("../api", async (original) => ({ ...(await original<typeof import("../api")>()),
  getModerationAlertStatus: vi.fn(), getModerationAudit: vi.fn() }));
afterEach(cleanup);

it("shows unconfigured delivery and failed mail retrying", async () => {
  vi.mocked(getModerationAlertStatus).mockResolvedValue({ delivery_configured: false, recipient_count: 0,
    email_sender: "console", pending_events: 2, pending_mail: 1, failed_mail: 1 });
  render(<ModerationAlerts onUnauthorized={() => {}} />);
  expect(await screen.findByText(/email delivery is not configured/)).toBeInTheDocument();
  expect(screen.getByText(/EMAIL\(S\) RETRYING/)).toBeInTheDocument();
});

it("renders reasons as text and loads older audit decisions", async () => {
  const entry = { id: "one", moderator_id: "moderator-id", moderator: "Moderator", target_kind: "observer" as const,
    target_id: "subject-id", action: "ban" as const, reason: "<script>unsafe</script>",
    previous_state: "active", new_state: "banned", created_at: "2026-01-01T00:00:00Z" };
  vi.mocked(getModerationAudit).mockResolvedValueOnce({ items: [entry], next_cursor: "one" })
    .mockResolvedValueOnce({ items: [{ ...entry, id: "two", reason: "Older decision" }], next_cursor: null });
  const { container } = render(<ModerationAudit onUnauthorized={() => {}} />);
  expect(await screen.findByText(entry.reason)).toBeInTheDocument();
  expect(container.querySelector("script")).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "OLDER DECISIONS" }));
  expect(await screen.findByText("Older decision")).toBeInTheDocument();
  expect(getModerationAudit).toHaveBeenLastCalledWith("one");
});
