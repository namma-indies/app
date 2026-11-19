// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("@capacitor/core", () => ({
  Capacitor: { isNativePlatform: () => false, isPluginAvailable: () => false, getPlatform: () => "web" },
}));
vi.mock("@capacitor/app", () => ({ App: { addListener: vi.fn() } }));
vi.mock("./screens/Capture", () => ({ default: () => <input aria-label="Capture draft" /> }));
vi.mock("./screens/Dex", () => ({ default: () => null }));
vi.mock("./screens/Admin", () => ({ default: () => null }));
vi.mock("./screens/SignIn", () => ({ default: () => <p>Sign-in gate</p> }));
vi.mock("./components/Celebration", () => ({ default: () => null }));
vi.mock("./components/ConsentReview", () => ({ default: ({ onAccepted, onDeleteAccount }: {
  onAccepted: () => void; onDeleteAccount: () => void;
}) => <div role="dialog" aria-label="Review policies"><button onClick={onAccepted}>Accept policies</button>
    <button onClick={onDeleteAccount}>Delete account</button></div> }));
vi.mock("./components/DeleteAccount", () => ({ default: ({ onClose }: { onClose: () => void }) =>
  <div role="dialog" aria-label="Delete account"><button onClick={onClose}>Cancel deletion</button></div> }));
vi.mock("./offline/queue", () => ({
  failedCount: async () => 0, flush: vi.fn(async () => {}), setOnFlushed: () => {}, setOnUnauthorized: () => {},
}));
vi.mock("./api", async (original) => ({ ...await original<typeof import("./api")>(), getDex: vi.fn(),
  getMe: vi.fn(async () => ({ is_moderator: false })) }));
vi.mock("./consentApi", async (original) => ({ ...await original<typeof import("./consentApi")>(), getAccountConsent: vi.fn() }));

import App from "./App";
import { getDex, HttpError } from "./api";
import { getAccountConsent, type ConsentPolicy } from "./consentApi";
import { CONSENT_REQUIRED_EVENT } from "./consentEvents";
import { flush } from "./offline/queue";

const policy: ConsentPolicy = { enabled: true, terms_version: "one", privacy_version: "one",
  community_guidelines_version: "one", minimum_age: null };

beforeEach(() => {
  vi.mocked(getDex).mockReset().mockResolvedValue({ sightings: [] });
  vi.mocked(getAccountConsent).mockReset().mockResolvedValue({ policy, required: false, accepted_at: "2026-11-03T00:00:00Z" });
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
});
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.restoreAllMocks(); });

it("shows the consent prompt instead of signing a stale account out", async () => {
  vi.mocked(getDex).mockRejectedValueOnce(new HttpError(428));
  render(<App />);
  expect(await screen.findByRole("dialog", { name: "Review policies" })).toBeInTheDocument();
  expect(screen.queryByText("Sign-in gate")).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Accept policies" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
});

it("preserves the current capture draft and resumes uploads after accepting", async () => {
  render(<App />);
  const draft = await screen.findByRole("textbox", { name: "Capture draft" });
  await userEvent.type(draft, "unfinished field note");
  act(() => { window.dispatchEvent(new Event(CONSENT_REQUIRED_EVENT)); });
  expect(screen.getByRole("dialog", { name: "Review policies" })).toBeInTheDocument();
  const flushesBefore = vi.mocked(flush).mock.calls.length;
  await userEvent.click(screen.getByRole("button", { name: "Accept policies" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(screen.getByRole("textbox", { name: "Capture draft" })).toBe(draft);
  expect(draft).toHaveValue("unfinished field note");
  expect(vi.mocked(flush).mock.calls.length).toBeGreaterThan(flushesBefore);
});

it("checks current consent on foreground resume without prompting already accepted accounts", async () => {
  render(<App />);
  await screen.findByRole("textbox", { name: "Capture draft" });
  act(() => { document.dispatchEvent(new Event("visibilitychange")); });
  await waitFor(() => expect(getAccountConsent).toHaveBeenCalledTimes(1));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  vi.mocked(getAccountConsent).mockResolvedValue({ policy, required: true, accepted_at: null });
  act(() => { document.dispatchEvent(new Event("visibilitychange")); });
  expect(await screen.findByRole("dialog", { name: "Review policies" })).toBeInTheDocument();
});

it("keeps in-app account deletion reachable before consent", async () => {
  vi.mocked(getDex).mockRejectedValueOnce(new HttpError(428));
  render(<App />);
  await userEvent.click(await screen.findByRole("button", { name: "Delete account" }));
  expect(screen.getByRole("dialog", { name: "Delete account" })).toBeInTheDocument();
  expect(screen.queryByRole("dialog", { name: "Review policies" })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Cancel deletion" }));
  expect(await screen.findByRole("dialog", { name: "Review policies" })).toBeInTheDocument();
});
