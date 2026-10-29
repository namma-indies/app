// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Transcript from "./Transcript";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const heard = [{ source: "note" as const, language: "en", text: "The brown one is Kaalu. He limps a little on the back leg." }];
// jsdom has no layout: say how tall the text is against the three lines it is clamped to.
const layout = (scroll: number, client: number, markBottom = 0) => {
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(scroll);
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(client);
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(markBottom);
};

describe("compact transcript", () => {
  it("clamps long text with a more button that opens and closes it, highlights kept", async () => {
    layout(120, 60);
    render(<Transcript compact transcripts={heard} highlights={[{ id: "n", quote: "The brown one is Kaalu", label: "Name: Kaalu" }]} />);
    const quote = screen.getByRole("button", { name: "more" }).closest("figure")!.querySelector("blockquote")!;
    expect(quote).toHaveClass("is-clamped");
    expect(screen.getByTitle("Name: Kaalu").tagName).toBe("MARK");
    await userEvent.click(screen.getByRole("button", { name: "more" }));
    expect(quote).not.toHaveClass("is-clamped");
    await userEvent.click(screen.getByRole("button", { name: "less" }));
    expect(quote).toHaveClass("is-clamped");
  });
  it("offers no more button when the text already fits", () => {
    layout(40, 60);
    render(<Transcript compact transcripts={heard} />);
    expect(screen.queryByRole("button", { name: "more" })).not.toBeInTheDocument();
  });
  it("opens up from the start when a highlighted word would be cut off", () => {
    layout(120, 60, 30); // the mark's bottom edge (offsetTop 0 + 30) is inside 60: stays clamped
    const { unmount } = render(<Transcript compact transcripts={heard} highlights={[{ quote: "back leg", label: "Condition: injured" }]} />);
    expect(screen.getByRole("button", { name: "more" })).toHaveAttribute("aria-expanded", "false");
    unmount();
    layout(120, 20, 30); // now it ends below the visible 20px
    render(<Transcript compact transcripts={heard} highlights={[{ quote: "back leg", label: "Condition: injured" }]} />);
    expect(screen.getByRole("button", { name: "less" })).toHaveAttribute("aria-expanded", "true");
  });
});
