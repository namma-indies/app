// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { Suggestion } from "../api";

import { decideSuggestion as decide } from "../api";
import SuggestedDetails from "./SuggestedDetails";

vi.mock("../api", async (original) => ({ ...await original<typeof import("../api")>(), decideSuggestion: vi.fn() }));
const decideSuggestion = vi.mocked(decide);

const SEX: Suggestion = { id: "s1", field: "sex", value: "female", quote: "She is asleep" };
const NAME: Suggestion = { id: "s2", field: "known_name", value: "Kaalu", quote: "Her name is Kaalu" };

afterEach(cleanup);
beforeEach(() => {
  decideSuggestion.mockReset().mockResolvedValue({ status: "accepted" });
});

function setup(suggestions = [SEX, NAME]) {
  const onDecided = vi.fn();
  render(<SuggestedDetails suggestions={suggestions} onDecided={onDecided} onUnauthorized={() => {}} />);
  return onDecided;
}

describe("SuggestedDetails", () => {
  it("marks the suggestion among the usual options and shows the words it came from", () => {
    setup();
    expect(screen.getByLabelText("♀ female (suggested)")).toHaveClass("suggested");
    expect(screen.getByText("♂ male")).not.toHaveClass("suggested");
    expect(screen.getByText("“She is asleep”")).toBeInTheDocument();
    expect(screen.getByLabelText("Kaalu (suggested)")).toBeInTheDocument();
  });

  it("saves nothing until an option is tapped", () => {
    setup();
    expect(decideSuggestion).not.toHaveBeenCalled();
  });

  it("taps the suggestion to accept it, and the answer disappears", async () => {
    const onDecided = setup();
    await act(async () => fireEvent.click(screen.getByLabelText("♀ female (suggested)")));
    expect(decideSuggestion).toHaveBeenCalledWith("s1", { verdict: "choose", value: "female" });
    expect(onDecided).toHaveBeenCalled();
    expect(screen.queryByLabelText("♀ female (suggested)")).not.toBeInTheDocument();
  });

  it("taps another option to correct it", async () => {
    setup([SEX]);
    await act(async () => fireEvent.click(screen.getByText("♂ male")));
    expect(decideSuggestion).toHaveBeenCalledWith("s1", { verdict: "choose", value: "male" });
  });

  it("says no without choosing anything", async () => {
    setup([NAME]);
    await act(async () => fireEvent.click(screen.getByText("not this")));
    expect(decideSuggestion).toHaveBeenCalledWith("s2", { verdict: "dismiss" });
    expect(screen.queryByText("FROM YOUR VOICE NOTE")).not.toBeInTheDocument();
  });

  it("keeps the choice open when saving fails", async () => {
    decideSuggestion.mockRejectedValue(new Error("offline"));
    setup([SEX]);
    fireEvent.click(screen.getByLabelText("♀ female (suggested)"));
    expect(await screen.findByText(/Couldn't save/)).toBeInTheDocument();
    expect(screen.getByLabelText("♀ female (suggested)")).toBeInTheDocument();
  });
});
