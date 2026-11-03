// @vitest-environment jsdom
import { afterEach, expect, it } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import AbuseSignals from "./AbuseSignals";

afterEach(cleanup);

it("shows hourly failures, upload spikes and the conservative email budget as signals for moderators", () => {
  render(<AbuseSignals data={{
    collection_started_at: "2026-11-03T00:00:00Z",
    hours: [{ hour: "2026-11-03T12:00:00Z", signups: 21, failed_signins: 51, auth_throttled: 40,
      uploads: 25, upload_throttled: 3, email_throttled: 2, email_failed: 1, signup_spike: true, signin_spike: true }],
    upload_spikes: [{ id: "demo", display_name: "Fast account", recent: 20, previous: 3 }],
    email_capacity: { daily_budget: 200, reserved_last_24h: 190, accepted_last_24h: 185, send_interval_s: 1, retry_at: null },
    thresholds: { signups_per_hour: 20, failed_signins_per_hour: 50, upload_spike_hour: 20 },
  }} />);
  expect(screen.getByText(/Account creation is above/)).toBeInTheDocument();
  expect(screen.getByText(/Failed sign-ins are above/)).toBeInTheDocument();
  expect(screen.getByText(/20 uploads in the last hour; 3 in the previous hour/)).toBeInTheDocument();
  expect(screen.getByText(/190\/200 attempts reserved/)).toBeInTheDocument();
  expect(screen.getByText(/do not trigger moderation automatically/)).toBeInTheDocument();
});
