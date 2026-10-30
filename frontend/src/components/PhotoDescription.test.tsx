// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import PhotoDescription, { describedAlt } from "./PhotoDescription";
import ChipGroup from "./ChipGroup";
import SuggestedDetails from "./SuggestedDetails";

const fetchMock = vi.fn();
beforeEach(() => { fetchMock.mockReset(); vi.stubGlobal("fetch", fetchMock); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const reply = (status: number, body: unknown = {}) => new Response(JSON.stringify(body), { status });
const TEXT = "A tan dog with a red collar. Its left ear is notched.";
const sent = () => JSON.parse(fetchMock.mock.calls[0][1].body as string);

describe("an AI photo description (#160)", () => {
  it("is labelled as AI's suggestion and marks the words a photo suggestion came from", () => {
    render(<PhotoDescription described={{ id: "d", text: TEXT, status: "open" }} onChanged={() => {}} onUnauthorized={() => {}}
      highlights={[{ id: "s", quote: "left ear is notched", label: "Ear notch: left" }]} />);
    expect(screen.getByText("AI · SUGGESTED")).toBeInTheDocument();
    expect(screen.getByText("left ear is notched").tagName).toBe("MARK");
    expect(screen.getByRole("button", { name: "Use" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Dismiss" })).toBeInTheDocument();
  });

  it("uses it as written", async () => {
    fetchMock.mockResolvedValue(reply(200, { status: "accepted", text: null }));
    const onChanged = vi.fn();
    render(<PhotoDescription described={{ id: "d", text: TEXT, status: "open" }} onChanged={onChanged} onUnauthorized={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Use" }));
    expect(fetchMock.mock.calls[0][0]).toMatch(/\/description\/d$/);
    expect(sent()).toEqual({ verdict: "use" });
    expect(onChanged).toHaveBeenCalledOnce();
  });

  it("lets the observer put it in their own words", async () => {
    fetchMock.mockResolvedValue(reply(200, { status: "edited", text: "Tan dog, red collar." }));
    render(<PhotoDescription described={{ id: "d", text: TEXT, status: "open" }} onChanged={() => {}} onUnauthorized={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Edit" }));
    const box = screen.getByLabelText("Describe the animal");
    expect(box).toHaveValue(TEXT);
    expect(box).toHaveAttribute("maxLength", "500");
    await userEvent.clear(box);
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    await userEvent.type(box, "Tan dog, red collar.");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(sent()).toEqual({ verdict: "edit", text: "Tan dog, red collar." });
  });

  it("says whose words they are once decided, and can still be removed", async () => {
    fetchMock.mockResolvedValue(reply(200, { status: "dismissed", text: null }));
    const { rerender } = render(<PhotoDescription described={{ id: "d", text: TEXT, status: "accepted" }} onChanged={() => {}} onUnauthorized={() => {}} />);
    expect(screen.getByText("AI-WRITTEN")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Use" })).not.toBeInTheDocument();
    rerender(<PhotoDescription described={{ id: "d", text: "Mine.", status: "edited" }} onChanged={() => {}} onUnauthorized={() => {}} />);
    expect(screen.getByText("EDITED BY YOU")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Remove" }));
    expect(sent()).toEqual({ verdict: "dismiss" });
  });

  it("says so when it could not save", async () => {
    fetchMock.mockResolvedValue(reply(500));
    render(<PhotoDescription described={{ id: "d", text: TEXT, status: "open" }} onChanged={() => {}} onUnauthorized={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Use" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t save that. Try again.");
  });

  it("is alt text only once its observer has taken it", () => {
    expect(describedAlt({ id: "d", text: TEXT, status: "open" })).toBeNull();
    expect(describedAlt({ id: "d", text: TEXT, status: "accepted" })).toBe(TEXT);
    expect(describedAlt({ id: "d", text: "Mine.", status: "edited" })).toBe("Mine.");
    expect(describedAlt(null)).toBeNull();
  });
});

describe("a suggestion from the photo says it is AI's", () => {
  it("groups photo suggestions apart from voice ones in the viewer", () => {
    render(<SuggestedDetails onDecided={() => {}} onUnauthorized={() => {}} suggestions={[
      { id: "v", field: "sex", value: "female", quote: "She is asleep" },
      { id: "p", field: "condition", value: "injured", quote: "a wound", description_id: "d" },
    ]} />);
    expect(screen.getByText("FROM YOUR VOICE NOTE")).toBeInTheDocument();
    expect(screen.getByText("FROM THE PHOTO · AI")).toBeInTheDocument();
  });

  it("marks a sheet chip AI when its suggestion came from the photo", () => {
    render(<ChipGroup label="Condition" field="condition" value={undefined} onChange={() => {}}
      options={[{ value: "healthy", label: "healthy" }, { value: "injured", label: "injured" }]}
      suggested={{ injured: ["p"], healthy: ["v"] }} fromPhoto={new Set(["injured"])} />);
    const chip = screen.getByRole("radio", { name: /^injured ?\(suggested by AI from the photo\)$/ });
    expect(chip.querySelector(".chip-ai")).toHaveTextContent("AI");
    expect(screen.getByRole("radio", { name: /^healthy ?\(suggested from your voice note\)$/ })).toBeInTheDocument();
  });
});
