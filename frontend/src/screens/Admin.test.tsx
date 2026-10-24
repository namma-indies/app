// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Admin from "./Admin";
vi.mock("./Stats", () => ({ default: () => <div>numbers screen</div> }));
vi.mock("./Moderation", () => ({ default: () => <div>flags screen</div> }));
vi.mock("./Social", () => ({ default: () => <div>posts screen</div> }));
afterEach(cleanup);

describe("the ADMIN tab", () => {
  it("opens on NUMBERS and switches between NUMBERS, FLAGS and POSTS", async () => {
    render(<Admin onUnauthorized={() => {}} />);
    expect(screen.getByText("numbers screen")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "FLAGS" }));
    expect(screen.getByText("flags screen")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "POSTS" }));
    expect(screen.getByText("posts screen")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "NUMBERS" }));
    expect(screen.getByText("numbers screen")).toBeInTheDocument();
  });
});
