export interface HeardText {
  source: "note" | "clip";
  language: string;
  text: string;
}

/** A word or phrase a suggestion came from, and what it suggested. */
export interface Highlight {
  quote: string;
  label: string;
  /** The suggestion it belongs to, so its chip can find these words (useSuggestionFlow). */
  id?: string;
}

const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/**
 * Where each quote sits in the text, first match, overlaps dropped. The API
 * checks a quote by its words alone and stores the transcript's own words
 * (app/suggestions.validate), so a quote is a literal piece of the text here:
 * matched ignoring case, with any run of whitespace equal to any other.
 */
export function highlightSpans(text: string, highlights: Highlight[]): { start: number; end: number; label: string; ids: string[] }[] {
  const found = highlights.flatMap(({ quote, label, id }) => {
    const words = quote.trim().split(/\s+/).filter(Boolean).map(escape);
    if (!words.length) return [];
    const match = new RegExp(words.join("\\s+"), "iu").exec(text);
    return match ? [{ start: match.index, end: match.index + match[0].length, label, ids: id ? [id] : [] }] : [];
  }).sort((a, b) => a.start - b.start || b.end - a.end);
  const kept: typeof found = [];
  for (const span of found) {
    const last = kept[kept.length - 1];
    if (last && span.start < last.end) {
      // Two suggestions read from overlapping words: one mark, both labels.
      if (span.end <= last.end && !last.label.includes(span.label)) {
        last.label += ` · ${span.label}`;
        last.ids.push(...span.ids);
      }
      continue;
    }
    kept.push({ ...span, ids: [...span.ids] });
  }
  return kept;
}

/**
 * What was heard in a voice note or a clip's sound, with the words each open
 * suggestion came from marked (#112). It shows the observer what the model
 * worked from, so a wrong suggestion can be traced to a misheard word.
 */
export default function Transcript({ transcripts, highlights = [] }: { transcripts: HeardText[]; highlights?: Highlight[] }) {
  if (!transcripts.length) return null;
  return <div className="transcript">
    {transcripts.map((t) => {
      const spans = highlightSpans(t.text, highlights);
      const parts: (string | JSX.Element)[] = [];
      let at = 0;
      spans.forEach((span, i) => {
        if (span.start > at) parts.push(t.text.slice(at, span.start));
        parts.push(<mark key={i} className="transcript-hit" title={span.label} data-fx={span.ids.join(" ") || undefined}>{t.text.slice(span.start, span.end)}</mark>);
        at = span.end;
      });
      parts.push(t.text.slice(at));
      return <figure key={t.source}>
        <figcaption className="spot-label">{t.source === "note" ? "WHAT YOU SAID" : "HEARD IN YOUR VIDEO"} · {t.language.toUpperCase()}</figcaption>
        <blockquote>{parts}</blockquote>
      </figure>;
    })}
    {highlights.length > 0 && <p className="hint">Highlighted words are where the suggestions came from.</p>}
  </div>;
}
