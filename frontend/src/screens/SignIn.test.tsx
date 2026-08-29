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
    await userEvent.type(screen.getByLabelText("Email"), "a@b.com");
    await userEvent.click(screen.getByText("Email me a link"));

    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/auth/email"),
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("does not tell a curious stranger the pilot is invite-only", async () => {
    // EMAIL_OPEN_SIGNUP is on in production -- any address is accepted. The
    // copy said "invite-only" for weeks after that flipped, which turns the
    // one link we hand to everyone into a door that reads as shut.
    render(<SignIn onSignedIn={vi.fn()} />);
    expect(screen.queryByText(/invite-only/i)).not.toBeInTheDocument();
  });

  it("does not ask for a Dognosis address -- Namma Indies is its own thing", async () => {
    render(<SignIn onSignedIn={vi.fn()} />);
    expect(screen.getByLabelText("Email")).not.toHaveAttribute(
      "placeholder",
      expect.stringContaining("dognosis"),
    );
    expect(screen.queryByText(/work email/i)).not.toBeInTheDocument();
  });

  it("leads with the Namma Indies mark, not a stock emoji", async () => {
    // The sign-in screen is the app's face before anyone is signed in.
    render(<SignIn onSignedIn={vi.fn()} />);
    expect(screen.getByRole("img", { name: "Namma Indies" })).toBeInTheDocument();
  });
});
