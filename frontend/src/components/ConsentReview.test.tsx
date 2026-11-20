// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("../consentApi", async (original) => ({
  ...await original<typeof import("../consentApi")>(), getAccountConsent: vi.fn(), acceptAccountConsent: vi.fn(),
}));
import ConsentReview from "./ConsentReview";
import { getAccountConsent, acceptAccountConsent, type ConsentPolicy } from "../consentApi";
import { HttpError } from "../api";

const policy: ConsentPolicy = { enabled: true, terms_version: "terms-one", privacy_version: "privacy-one",
  community_guidelines_version: "guidelines-one", minimum_age: null };
const props = () => ({ onAccepted: vi.fn(), onSignOut: vi.fn(), onDeleteAccount: vi.fn(), onUnauthorized: vi.fn() });

beforeEach(() => {
  vi.mocked(getAccountConsent).mockReset().mockResolvedValue({ policy, required: true, accepted_at: null });
  vi.mocked(acceptAccountConsent).mockReset().mockResolvedValue({ policy, required: false, accepted_at: "2026-11-03T00:00:00Z" });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

async function agree() {
  await userEvent.click(await screen.findByRole("checkbox", { name: /I agree to the Terms of Service and Community Guidelines/ }));
  await userEvent.click(screen.getByRole("checkbox", { name: /I acknowledge the Privacy Policy/ }));
}

it("requires affirmative current-version agreement before continuing", async () => {
  const callbacks = props(); render(<ConsentReview {...callbacks} />);
  const submit = await screen.findByRole("button", { name: "Agree and continue" });
  expect(submit).toBeDisabled();
  for (const input of screen.getAllByRole("checkbox")) expect(input).not.toBeChecked();
  await agree(); await userEvent.click(submit);
  expect(acceptAccountConsent).toHaveBeenCalledWith(policy, {
    agree_terms_and_guidelines: true, acknowledge_privacy: true, age_confirmed: false,
  });
  expect(callbacks.onAccepted).toHaveBeenCalledOnce();
});

it("closes a stale prompt when another visit has already accepted", async () => {
  vi.mocked(getAccountConsent).mockResolvedValue({ policy, required: false, accepted_at: "2026-11-03T00:00:00Z" });
  const callbacks = props(); render(<ConsentReview {...callbacks} />);
  await waitFor(() => expect(callbacks.onAccepted).toHaveBeenCalledOnce());
  expect(acceptAccountConsent).not.toHaveBeenCalled();
});

it("retains help, sign-out and account deletion if policies cannot be loaded", async () => {
  vi.mocked(getAccountConsent).mockRejectedValue(new TypeError("offline"));
  const callbacks = props(); render(<ConsentReview {...callbacks} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t load the current policies");
  for (const name of ["Terms of Service", "Community Guidelines", "Privacy Policy", "Grievances", "Support"]) {
    expect(screen.getByRole("link", { name })).toBeInTheDocument();
  }
  await userEvent.click(screen.getByRole("button", { name: "Delete account" }));
  await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
  expect(callbacks.onDeleteAccount).toHaveBeenCalledOnce();
  expect(callbacks.onSignOut).toHaveBeenCalledOnce();
  expect(acceptAccountConsent).not.toHaveBeenCalled();
});

it("asks again after a version changes while the prompt is open", async () => {
  const callbacks = props(); render(<ConsentReview {...callbacks} />);
  await agree();
  vi.mocked(acceptAccountConsent).mockRejectedValueOnce(new HttpError(428));
  vi.mocked(getAccountConsent).mockResolvedValue({ policy: { ...policy, privacy_version: "privacy-two" }, required: true, accepted_at: null });
  await userEvent.click(screen.getByRole("button", { name: "Agree and continue" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("The policies changed");
  for (const input of screen.getAllByRole("checkbox")) expect(input).not.toBeChecked();
  expect(callbacks.onAccepted).not.toHaveBeenCalled();
  await agree(); await userEvent.click(screen.getByRole("button", { name: "Agree and continue" }));
  expect(vi.mocked(acceptAccountConsent).mock.calls[1][0].privacy_version).toBe("privacy-two");
  expect(callbacks.onAccepted).toHaveBeenCalledOnce();
});

it("cannot be dismissed with Escape before acceptance", async () => {
  const callbacks = props(); render(<ConsentReview {...callbacks} />);
  await screen.findByRole("checkbox", { name: /I acknowledge the Privacy Policy/ });
  const cancel = new Event("cancel", { cancelable: true });
  screen.getByRole("dialog").dispatchEvent(cancel);
  expect(cancel.defaultPrevented).toBe(true);
  expect(callbacks.onAccepted).not.toHaveBeenCalled();
});
