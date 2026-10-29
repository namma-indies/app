// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { join } from "node:path";
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
    expect(highlightSpans("a (b) c", [{ quote: "(b)", label: "x" }])).toEqual([{ start: 2, end: 5, label: "x", ids: [], used: false }]);
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

  it("marks a used suggestion's words too, quieter, and says what the dotted ones mean", () => {
    render(<Transcript transcripts={[{ source: "note", language: "en", text: heard }]} highlights={[
      { id: "a", quote: "She looks healthy", label: "Condition: healthy", used: true },
      { id: "b", quote: "People call her", label: "Name: Kalu" },
    ]} />);
    const used = screen.getByText("She looks healthy");
    expect(used).toHaveClass("is-used");
    expect(used).toHaveAttribute("title", "Condition: healthy (filled in)");
    expect(used).toHaveAttribute("data-fx", "a");
    expect(screen.getByText(/People call her/)).not.toHaveClass("is-used");
    expect(screen.getByText(/Dotted ones are already filled in/)).toBeInTheDocument();
  });

  it("says nothing about dotted marks when none is used", () => {
    render(<Transcript transcripts={[{ source: "note", language: "en", text: heard }]}
      highlights={[{ quote: "She looks healthy", label: "Condition: healthy" }]} />);
    expect(screen.queryByText(/Dotted/)).not.toBeInTheDocument();
  });

  it("keeps a mark shared by an open and a used suggestion at full strength", () => {
    const text = "She is a female.";
    const [span] = highlightSpans(text, [
      { id: "u", quote: "She is a female", label: "Sex: female", used: true },
      { id: "o", quote: "She is a", label: "Ear notch: left" },
    ]);
    expect(span.used).toBe(false);
    expect(span.ids).toEqual(["u", "o"]);
  });

  it("renders nothing when nothing was heard", () => {
    const { container } = render(<Transcript transcripts={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("the transcript's spacing", () => {
  it("leaves room between lines for a highlight's underline, so the next line's highlight can't cover it (#139)", () => {
    // The real stylesheet (vitest blanks CSS imports, so read the file).
    const style = document.createElement("style");
    style.textContent = readFileSync(join(__dirname, "../styles.css"), "utf8");
    document.head.appendChild(style);
    try {
      const { container } = render(<Transcript transcripts={[{ source: "note", language: "en", text: "She is a female." }]}
        highlights={[{ quote: "She is a female", label: "Sex: female" }]} />);
      // A highlight is its text (about 1.25em tall in Quicksand) plus a 2px underline, 22px at 16px: the
      // line has to be at least that tall. Unitless, so it scales with the text.
      const lineHeight = getComputedStyle(container.querySelector("blockquote")!).lineHeight;
      expect(lineHeight).toMatch(/^\d+(\.\d+)?$/);
      expect(Number(lineHeight) * 16).toBeGreaterThanOrEqual(1.25 * 16 + 2);
    } finally { style.remove(); }
  });
});
