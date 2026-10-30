// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import SightingFrames from "./SightingFrames";

afterEach(cleanup);
const frames = (n: number) => Array.from({ length: n }, (_, i) => ({ url: `f${i}.webp`, thumb_url: `f${i}_t.webp` }));

describe("SightingFrames", () => {
  it("shows a lone photo on its own, with no strip", () => {
    render(<SightingFrames photos={frames(1)} noun="Dog" />);
    expect(screen.getByRole("img", { name: "Dog sighting" })).toHaveAttribute("src", "f0.webp");
    expect(screen.queryByRole("group", { name: "Frames" })).not.toBeInTheDocument();
  });

  it("leads with the first frame and lists every frame as a thumbnail", () => {
    render(<SightingFrames photos={frames(6)} noun="Dog" />);
    expect(screen.getByRole("img", { name: "Dog sighting, frame 1 of 6" })).toHaveAttribute("src", "f0.webp");
    expect(screen.getAllByRole("button", { name: /Show frame/ })).toHaveLength(6);
    expect(screen.getByRole("button", { name: "Show frame 1 of 6" })).toHaveAttribute("aria-pressed", "true");
  });

  it("tapping a thumbnail leads with that frame", async () => {
    render(<SightingFrames photos={frames(3)} noun="Dog" />);
    await userEvent.click(screen.getByRole("button", { name: "Show frame 3 of 3" }));
    expect(screen.getByRole("img", { name: "Dog sighting, frame 3 of 3" })).toHaveAttribute("src", "f2.webp");
    expect(screen.getByRole("button", { name: "Show frame 3 of 3" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Show frame 1 of 3" })).toHaveAttribute("aria-pressed", "false");
  });

  it("renders nothing without photos", () => {
    const { container } = render(<SightingFrames photos={[]} noun="Dog" />);
    expect(container).toBeEmptyDOMElement();
  });
});
