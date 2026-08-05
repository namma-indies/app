// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import SignIn from "./SignIn";

afterEach(cleanup);

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ ok: true }),
    }),
  );
});

describe("SignIn", () => {
  it("sends credentials on the passcode submit -- otherwise the cross-origin session cookie is never even considered", async () => {
    render(<SignIn onSignedIn={vi.fn()} />);
    await userEvent.click(screen.getByText("Use a passcode"));
    await userEvent.type(screen.getByLabelText("Your name"), "tester");
    await userEvent.type(screen.getByLabelText("Passcode"), "secret");
    await userEvent.click(screen.getByText("Join with passcode"));

    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/auth/join"),
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("sends credentials on the email submit too", async () => {
    render(<SignIn onSignedIn={vi.fn()} />);
    await userEvent.type(screen.getByLabelText("Work email"), "a@b.com");
    await userEvent.click(screen.getByText("Email me a link"));

    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/auth/email"),
      expect.objectContaining({ credentials: "include" }),
    );
  });
});
