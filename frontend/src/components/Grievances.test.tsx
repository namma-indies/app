// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Grievances from "./Grievances";
import { decideGrievance, getGrievances, type Grievance } from "../api";

vi.mock("../api", async (original) => ({ ...(await original<typeof import("../api")>()),
  getGrievances: vi.fn(), decideGrievance: vi.fn() }));
afterEach(cleanup);
beforeEach(() => vi.clearAllMocks());

const legalNote = "Provisional tracking targets: confirm every deadline with a lawyer.";
function complaint(over: Partial<Grievance> = {}): Grievance {
  return { id: "complaint-id", reference: "NI-REFERENCE", kind: "intimate_imagery", target: "sighting-id",
    sighting_id: "sighting-id", details: "Private concern", contact_email: "person@example.invalid", status: "open",
    received_at: "2026-01-01T00:00:00Z", acknowledge_due_at: "2026-01-02T00:00:00Z",
    resolve_due_at: "2026-01-08T00:00:00Z", removal_due_at: "2026-01-01T02:00:00Z",
    acknowledged_at: null, acknowledgement_error: null, removed_at: null, resolved_at: null, resolution_reason: null,
    age_seconds: 100, acknowledgement_overdue: true, resolution_overdue: true, removal_overdue: true,
    next_due_at: "2026-01-01T02:00:00Z", legal_confirmation_required: true, ...over };
}

it("shows the reference, urgent type, overdue clocks and acknowledgement failures", async () => {
  vi.mocked(getGrievances).mockResolvedValue({ items: [complaint({ acknowledgement_error: "TimeoutError" })], legal_note: legalNote });
  render(<Grievances onUnauthorized={() => {}} />);
  expect(await screen.findByText(/NON-CONSENSUAL INTIMATE IMAGERY/)).toBeInTheDocument();
  expect(screen.getByText(/NI-REFERENCE/)).toBeInTheDocument();
  expect(screen.getByText(legalNote)).toBeInTheDocument();
  expect(screen.getAllByText(/OVERDUE/)).toHaveLength(3);
  expect(screen.getByText(/email failed; retrying/)).toBeInTheDocument();
});

it("requires a reason and records the hide before keeping a complaint open", async () => {
  vi.mocked(getGrievances).mockResolvedValueOnce({ items: [complaint()], legal_note: legalNote })
    .mockResolvedValueOnce({ items: [complaint({ status: "in_review", removed_at: "2026-01-01T01:00:00Z" })], legal_note: legalNote });
  vi.mocked(decideGrievance).mockResolvedValue(undefined);
  render(<Grievances onUnauthorized={() => {}} />);
  expect(await screen.findByRole("button", { name: "HIDE LINKED CONTENT" })).toBeDisabled();
  await userEvent.type(screen.getByLabelText("Decision reason"), "Verified concern");
  await userEvent.click(screen.getByRole("button", { name: "HIDE LINKED CONTENT" }));
  expect(decideGrievance).toHaveBeenCalledWith("complaint-id", "hide", "Verified concern", "sighting-id");
  expect(await screen.findByText(/IN_REVIEW/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "MARK RESOLVED" })).toBeInTheDocument();
});

it("shows closed complaints still waiting for acknowledgement without more decisions", async () => {
  vi.mocked(getGrievances).mockResolvedValue({ items: [complaint({ status: "resolved", resolved_at: "2026-01-03T00:00:00Z",
    resolution_reason: "Reply sent" })], legal_note: legalNote });
  render(<Grievances onUnauthorized={() => {}} />);
  expect(await screen.findByText(/NO REMOVAL RECORDED/)).toBeInTheDocument();
  expect(screen.getByText(/Acknowledgement email queued/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "MARK RESOLVED" })).toBeNull();
});

it("searches by reference including closed records and renders evidence as text", async () => {
  vi.mocked(getGrievances).mockResolvedValue({ items: [complaint({ details: '<img src=x onerror="alert(1)">' })], legal_note: legalNote });
  const { container } = render(<Grievances onUnauthorized={() => {}} />);
  await screen.findByText(/<img src=x/);
  expect(container.querySelector("img")).toBeNull();
  await userEvent.type(screen.getByLabelText("Reference number"), "ni-reference");
  await userEvent.click(screen.getByRole("button", { name: "FIND" }));
  await waitFor(() => expect(getGrievances).toHaveBeenLastCalledWith({ includeClosed: false, reference: "NI-REFERENCE" }));
});
