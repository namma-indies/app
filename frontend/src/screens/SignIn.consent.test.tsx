// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import SignIn from "./SignIn";
import type { ConsentPolicy } from "../consentApi";

let policy: ConsentPolicy;
const response = (body: unknown, status = 200) => ({ ok: status < 400, status, json: async () => body }) as Response;

beforeEach(() => {
  policy = { enabled: true, terms_version: "terms-one", privacy_version: "privacy-one",
    community_guidelines_version: "guidelines-one", minimum_age: null };
  vi.stubGlobal("fetch", vi.fn(async (path: string) => path.endsWith("/auth/consent")
    ? response(policy) : response({ ok: true, message: "Follow the instructions in your inbox." })));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

async function creating() {
  render(<SignIn onSignedIn={vi.fn()} />);
  await userEvent.click(await screen.findByRole("button", { name: "New here? Create an account" }));
  await userEvent.type(screen.getByLabelText("Email"), "person@example.invalid");
}

async function agree() {
  await userEvent.click(screen.getByRole("checkbox", { name: /I agree to the Terms of Service and Community Guidelines/ }));
  await userEvent.click(screen.getByRole("checkbox", { name: /I acknowledge the Privacy Policy/ }));
}

function posted(path: string) {
  return vi.mocked(fetch).mock.calls.filter(([url, init]) => String(url).endsWith(path) && init?.method === "POST");
}

describe("signup agreement", () => {
  it("lets returning email users sign in without agreeing again", async () => {
    render(<SignIn onSignedIn={vi.fn()} />);
    await screen.findByRole("button", { name: "New here? Create an account" });
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Email"), "person@example.invalid");
    await userEvent.click(screen.getByRole("button", { name: "Email me a link" }));
    const body = posted("/auth/email")[0][1]?.body as URLSearchParams;
    expect(body.get("create_account")).toBe("false");
    expect(body.has("agree_terms_and_guidelines")).toBe(false);
    expect(await screen.findByRole("button", { name: "Create an account" })).toBeInTheDocument();
  });

  it("requires separate affirmative agreement and privacy acknowledgment before creating", async () => {
    await creating();
    const submit = screen.getByRole("button", { name: "Email me a link" });
    const [terms, privacy] = screen.getAllByRole("checkbox");
    expect(terms).not.toBeChecked(); expect(privacy).not.toBeChecked();
    expect(submit).toBeDisabled();
    await userEvent.click(terms);
    expect(submit).toBeDisabled();
    await userEvent.click(privacy);
    expect(submit).toBeEnabled();
    expect(screen.queryByText(/I am at least/)).not.toBeInTheDocument();
    await userEvent.click(submit);
    const body = posted("/auth/email")[0][1]?.body as URLSearchParams;
    expect(body.get("create_account")).toBe("true");
    expect(body.get("agree_terms_and_guidelines")).toBe("true");
    expect(body.get("acknowledge_privacy")).toBe("true");
    expect(body.get("terms_version")).toBe("terms-one");
    expect(body.get("community_guidelines_version")).toBe("guidelines-one");
    expect(body.get("privacy_version")).toBe("privacy-one");
    expect(body.has("minimum_age")).toBe(false);
  });

  it("requires the same agreement before a passcode join", async () => {
    const signedIn = vi.fn();
    render(<SignIn onSignedIn={signedIn} />);
    await screen.findByRole("button", { name: "New here? Create an account" });
    await userEvent.click(screen.getByRole("button", { name: "Use a passcode" }));
    await userEvent.type(screen.getByLabelText("Your name"), "Tester");
    await userEvent.type(screen.getByLabelText("Passcode"), "sample");
    const submit = screen.getByRole("button", { name: "Join with passcode" });
    expect(submit).toBeDisabled();
    await agree(); await userEvent.click(submit);
    expect(signedIn).toHaveBeenCalledOnce();
    const body = posted("/auth/join")[0][1]?.body as URLSearchParams;
    expect(body.get("terms_version")).toBe("terms-one");
    expect(body.get("acknowledge_privacy")).toBe("true");
  });

  it("only adds an age assertion when configured and sends the age actually shown", async () => {
    policy.minimum_age = 21;
    await creating();
    await agree();
    const submit = screen.getByRole("button", { name: "Email me a link" });
    expect(submit).toBeDisabled();
    await userEvent.click(screen.getByRole("checkbox", { name: "I am at least 21 years old." }));
    await userEvent.click(submit);
    const body = posted("/auth/email")[0][1]?.body as URLSearchParams;
    expect(body.get("minimum_age")).toBe("21");
    expect(body.get("age_confirmed")).toBe("true");
  });

  it("clears checkboxes and uses current versions after a stale submission is refused", async () => {
    let submits = 0;
    vi.mocked(fetch).mockImplementation(async (url, init) => {
      if (String(url).endsWith("/auth/consent")) return response(policy);
      if (init?.method === "POST" && ++submits === 1) {
        return response({ ok: false, error: "The policies changed.", consent_required: true,
          consent: { ...policy, terms_version: "terms-two" } }, 428);
      }
      return response({ ok: true, message: "Follow the instructions in your inbox." });
    });
    await creating(); await agree();
    await userEvent.click(screen.getByRole("button", { name: "Email me a link" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The policies changed.");
    expect(screen.getByRole("button", { name: "Email me a link" })).toBeDisabled();
    for (const checkbox of screen.getAllByRole("checkbox")) expect(checkbox).not.toBeChecked();
    await agree(); await userEvent.click(screen.getByRole("button", { name: "Email me a link" }));
    const body = posted("/auth/email")[1][1]?.body as URLSearchParams;
    expect(body.get("terms_version")).toBe("terms-two");
  });

  it("does not silently create without loading the current policy configuration", async () => {
    vi.mocked(fetch).mockRejectedValue(new TypeError("offline"));
    render(<SignIn onSignedIn={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t load the current policies");
    expect(screen.getByRole("button", { name: "Email me a link" })).toBeDisabled();
    expect(posted("/auth/email")).toHaveLength(0);
    vi.mocked(fetch).mockResolvedValue(response(policy));
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Email me a link" })).toBeEnabled());
  });
});
