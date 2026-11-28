// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import AccountMenu from "./AccountMenu";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function open() {
  const handlers = { onBlocked: vi.fn(), onDeleteAccount: vi.fn(), onSignOut: vi.fn() };
  render(<AccountMenu {...handlers} />);
  return handlers;
}

it("keeps the policies out of sight until asked for", () => {
  open();
  expect(screen.queryByRole("link", { name: "Terms of Service" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "menu" })).toBeInTheDocument();
});

it("holds every policy, blocking, deletion and sign-out", async () => {
  open();
  await userEvent.click(screen.getByRole("button", { name: "menu" }));
  for (const name of ["Terms of Service", "Community Guidelines", "Privacy Policy", "Grievances", "Support"]) {
    expect(screen.getByRole("link", { name })).toBeInTheDocument();
  }
  for (const name of ["Blocked people", "Delete account", "Sign out"]) {
    expect(screen.getByRole("button", { name })).toBeInTheDocument();
  }
});

it("closes on the way to whatever was chosen", async () => {
  const h = open();
  await userEvent.click(screen.getByRole("button", { name: "menu" }));
  await userEvent.click(screen.getByRole("button", { name: "Delete account" }));
  expect(h.onDeleteAccount).toHaveBeenCalled();
  expect(screen.queryByRole("dialog", { name: "Menu" })).not.toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: "menu" }));
  await userEvent.click(screen.getByRole("button", { name: "Blocked people" }));
  expect(h.onBlocked).toHaveBeenCalled();
});

it("asks before signing out", async () => {
  const h = open();
  const confirmSpy = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
  await userEvent.click(screen.getByRole("button", { name: "menu" }));
  await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
  expect(h.onSignOut).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
  expect(h.onSignOut).toHaveBeenCalled();
  expect(confirmSpy).toHaveBeenCalledTimes(2);
});
