// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import ReportSheet from "./ReportSheet";

afterEach(cleanup);

describe("ReportSheet species wording", () => {
  it.each([
    ["cat", "Puts this cat at risk", "Not a cat"],
    ["dog", "Puts this dog at risk", "Not a dog"],
    [null, "Puts this animal at risk", "Not an animal"],
  ] as const)("names the animal for %s", (species, risk, not) => {
    render(<ReportSheet sightingId="s" species={species} onClose={() => {}} onReported={() => {}} onUnauthorized={() => {}} />);
    expect(screen.getByRole("radio", { name: risk })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: not })).toBeInTheDocument();
  });
});
