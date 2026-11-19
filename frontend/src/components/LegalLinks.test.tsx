// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, within } from "@testing-library/react";

vi.mock("../apiBase", () => ({ API_BASE: "/backend" }));
import LegalLinks from "./LegalLinks";

afterEach(cleanup);

it("sends every policy and help link to the backend origin, including in native apps", () => {
  render(<LegalLinks><button>delete account</button></LegalLinks>);
  const nav = within(screen.getByRole("navigation", { name: "Policies and support" }));
  for (const [label, path] of [
    ["Terms of Service", "/policies/terms"],
    ["Community Guidelines", "/policies/community-guidelines"],
    ["Privacy Policy", "/policies/privacy"],
    ["Grievances", "/policies/grievance"],
    ["Support", "/support"],
  ]) {
    const link = nav.getByRole("link", { name: label });
    expect(link).toHaveAttribute("href", `/backend${path}`);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noreferrer");
  }
  expect(nav.getByRole("button", { name: "delete account" })).toBeInTheDocument();
});
