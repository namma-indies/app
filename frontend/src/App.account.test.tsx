// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("@capacitor/core", () => ({
  Capacitor: { isNativePlatform: () => true, isPluginAvailable: () => false, getPlatform: () => "ios" },
}));
vi.mock("@capacitor/app", () => ({ App: { getInfo: async () => ({ version: "1", build: "1" }), addListener: vi.fn() } }));
vi.mock("./screens/Capture", () => ({ default: () => null }));
vi.mock("./screens/Dex", () => ({ default: () => null }));
vi.mock("./screens/Admin", () => ({ default: () => null }));
vi.mock("./screens/SignIn", () => ({ default: ({ onSignedIn }: { onSignedIn: () => void }) =>
  <button onClick={onSignedIn}>signed in</button> }));
vi.mock("./components/Celebration", () => ({ default: () => null }));
vi.mock("./offline/queue", () => ({
  nextRetryAt: async () => null, failedCount: async () => 0, flush: async () => {}, setOnFlushed: () => {}, setOnUnauthorized: () => {},
}));
vi.mock("./api", async (original) => {
  const real = await original<typeof import("./api")>();
  return { ...real, getDex: vi.fn(), getMe: vi.fn(async () => ({ is_moderator: false })), getAccount: vi.fn() };
});

import App from "./App";
import { getAccount, getDex, HttpError, UnauthorizedError } from "./api";

afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("an account scheduled for deletion signing back in (#179)", () => {
  it("lands on Keep my account, not on the sign-in gate", async () => {
    vi.mocked(getDex).mockRejectedValueOnce(new UnauthorizedError()).mockRejectedValue(new HttpError(403));
    vi.mocked(getAccount).mockResolvedValue({ status: "pending_deletion", purge_after: "2026-11-09T10:00:00Z",
      can_cancel: true, has_email: true, grace_days: 7, counts: { uploads: 1, sightings: 1, voice_notes: 0 } });
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "signed in" }));
    expect(await screen.findByRole("button", { name: "Keep my account" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "signed in" })).not.toBeInTheDocument();
  });
});

describe("a suspended account (#180)", () => {
  it("is told so, and can still delete itself", async () => {
    vi.mocked(getDex).mockRejectedValue(new HttpError(403));
    vi.mocked(getAccount).mockResolvedValue({ status: "active", purge_after: null, can_cancel: false, has_email: true,
      grace_days: 7, suspended: true, counts: { uploads: 1, sightings: 1, voice_notes: 0 } });
    render(<App />);
    expect(await screen.findByText("Your account has been suspended")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "delete account" })).toBeInTheDocument();
  });
});
