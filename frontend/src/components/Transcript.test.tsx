// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import Transcript, { highlightSpans } from "./Transcript";

afterEach(cleanup);

// whisper-server breaks its text into lines mid-sentence.
const heard = " She is a white female with a cut left ear. People call her\n Kalu.\n She looks healthy.\n";

describe("highlighting the words a suggestion came from", () => {
  it("finds a quote ignoring case and however the whitespace was broken", () => {
    const [span] = highlightSpans(heard, [{ quote: "people call her kalu", label: "Name: Kalu" }]);
    expect(heard.slice(span.start, span.end)).toBe("People call her\n Kalu");
  });

  it("finds the note's own words with their punctuation, as the API stores them (#115)", () => {
    // The API keeps the transcript's words for a suggestion's quote even when the model left a comma out
    // or straightened an apostrophe, so a literal copy is all this has to find.
    const text = "She is a female. Her name is Kaalu, her left ear is notched. He\u2019s calm.";
    const spans = highlightSpans(text, [
      { quote: "Her name is Kaalu, her left ear is notched", label: "Name: Kaalu" },
      { quote: "He\u2019s calm", label: "Sex: male" },
    ]);
    expect(spans.map((span) => text.slice(span.start, span.end))).toEqual([
      "Her name is Kaalu, her left ear is notched", "He\u2019s calm"]);
  });

  it("marks nothing for a quote that is not there, and never matches it as a pattern", () => {
    expect(highlightSpans(heard, [{ quote: "limping", label: "Condition: injured" }])).toEqual([]);
    expect(highlightSpans("a (b) c", [{ quote: "(b)", label: "x" }])).toEqual([{ start: 2, end: 5, label: "x", ids: [] }]);
  });

  it("keeps every suggestion's id on a shared mark, even when their labels read the same", () => {
    const text = "The brown one and the black one are both female.";
    const spans = highlightSpans(text, [
      { id: "a", quote: "The brown one and the black one are both female", label: "Sex: female" },
      { id: "b", quote: "the black one are both female", label: "Sex: female" },
    ]);
    expect(spans).toHaveLength(1);
    expect(spans[0].ids).toEqual(["a", "b"]);
    expect(spans[0].label).toBe("Sex: female");
  });

  it("keeps one mark for overlapping quotes and names both", () => {
    const spans = highlightSpans(heard, [
      { quote: "She is a white female with a cut left ear", label: "Ear notch: left" },
      { quote: "She is a white female", label: "Sex: female" },
    ]);
    expect(spans).toHaveLength(1);
    expect(spans[0].label).toBe("Ear notch: left · Sex: female");
  });

  it("shows what was heard with each suggestion's words marked", () => {
    render(<Transcript transcripts={[{ source: "note", language: "en", text: heard }]}
      highlights={[{ quote: "She looks healthy", label: "Condition: healthy" }]} />);
    expect(screen.getByText(/WHAT YOU SAID · EN/)).toBeInTheDocument();
    const mark = screen.getByText("She looks healthy");
    expect(mark.tagName).toBe("MARK");
    expect(mark).toHaveAttribute("title", "Condition: healthy");
  });

  it("renders nothing when nothing was heard", () => {
    const { container } = render(<Transcript transcripts={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});
