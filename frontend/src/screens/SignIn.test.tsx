// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import SignIn from "./SignIn";

vi.mock("../authProof", () => ({ passcodeProof: vi.fn().mockResolvedValue({ challenge: "signed-work", solution: "42" }) }));

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
    const body = vi.mocked(fetch).mock.calls.find(([path]) => String(path).endsWith("/auth/join"))?.[1]?.body as URLSearchParams;
    expect(body.get("challenge")).toBe("signed-work");
    expect(body.get("solution")).toBe("42");
  });

  it("sends credentials on the email submit too", async () => {
    render(<SignIn onSignedIn={vi.fn()} />);
    await userEvent.type(screen.getByLabelText("Email"), "person@example.test");
    await userEvent.click(screen.getByText("Email me a link"));

    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/auth/email"),
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("does not tell a curious stranger the pilot is invite-only", async () => {
    // Enrollment is enforced by the server's switch and allowlist.
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

  it("shows the server's wait time when sign-in is throttled", async () => {
    vi.mocked(fetch).mockResolvedValueOnce({ ok: true, json: async () => ({ store_reviewer_enabled: false }) } as Response);
    vi.mocked(fetch).mockResolvedValueOnce({ ok: false, json: async () => ({ detail: "Too many requests. Try again in 60 seconds." }) } as Response);
    render(<SignIn onSignedIn={vi.fn()} />);
    await userEvent.type(screen.getByLabelText("Email"), "person@example.test");
    await userEvent.click(screen.getByText("Email me a link"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Try again in 60 seconds");
  });

  it("keeps the form usable when a 422 contains structured validation details", async () => {
    vi.mocked(fetch).mockResolvedValueOnce({ ok: true, json: async () => ({ store_reviewer_enabled: false }) } as Response);
    vi.mocked(fetch).mockResolvedValueOnce({ ok: false, status: 422,
      json: async () => ({ detail: [{ loc: ["body", "email"], msg: "too long", type: "string_too_long" }] }),
    } as Response);
    render(<SignIn onSignedIn={vi.fn()} />);
    await userEvent.type(screen.getByLabelText("Email"), "person@example.test");
    await userEvent.click(screen.getByText("Email me a link"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Please check the fields");
    expect(screen.getByText("Email me a link")).toBeEnabled();
  });

  it("hides reviewer access unless the server explicitly enables it", async () => {
    render(<SignIn onSignedIn={vi.fn()} />);
    expect(screen.queryByText("Store reviewer")).not.toBeInTheDocument();
  });

  it("signs the store reviewer into the demo account with a code and no email", async () => {
    vi.mocked(fetch).mockResolvedValueOnce({ ok: true, json: async () => ({ store_reviewer_enabled: true }) } as Response);
    const signedIn = vi.fn();
    render(<SignIn onSignedIn={signedIn} />);
    await userEvent.click(await screen.findByText("Store reviewer"));
    await userEvent.type(screen.getByLabelText("Reviewer code"), "a-review-code-for-tests");
    await userEvent.click(screen.getByText("Sign in as reviewer"));
    const call = vi.mocked(fetch).mock.calls.find(([path]) => String(path).endsWith("/auth/reviewer"));
    expect(call?.[1]).toMatchObject({ credentials: "include", method: "POST" });
    const form = call?.[1]?.body as URLSearchParams;
    expect(form.get("secret")).toBe("a-review-code-for-tests");
    expect(form.has("email")).toBe(false);
    expect(signedIn).toHaveBeenCalledOnce();
  });
});
