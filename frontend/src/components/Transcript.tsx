import { useLayoutEffect, useRef, useState } from "react";

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
  /** Already answered and saved: still marked, but quieter than one that still needs you. */
  used?: boolean;
}

const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/**
 * Where each quote sits in the text, first match, overlaps dropped. The API
 * checks a quote by its words alone and stores the transcript's own words
 * (app/suggestions.validate), so a quote is a literal piece of the text here:
 * matched ignoring case, with any run of whitespace equal to any other.
 */
export function highlightSpans(text: string, highlights: Highlight[]): { start: number; end: number; label: string; ids: string[]; used: boolean }[] {
  const found = highlights.flatMap(({ quote, label, id, used = false }) => {
    const words = quote.trim().split(/\s+/).filter(Boolean).map(escape);
    if (!words.length) return [];
    const match = new RegExp(words.join("\\s+"), "iu").exec(text);
    return match ? [{ start: match.index, end: match.index + match[0].length, label, ids: id ? [id] : [], used }] : [];
  }).sort((a, b) => a.start - b.start || b.end - a.end);
  const kept: typeof found = [];
  for (const span of found) {
    const last = kept[kept.length - 1];
    if (last && span.start < last.end) {
      // Two suggestions read from overlapping words: one mark, both labels and
      // both ids (the same label can belong to two different suggestions).
      if (span.end <= last.end) {
        if (!last.label.includes(span.label)) last.label += ` · ${span.label}`;
        last.ids.push(...span.ids);
        last.used = last.used && span.used; // one still open keeps the mark at full strength
      }
      continue;
    }
    kept.push({ ...span, ids: [...span.ids] });
  }
  return kept;
}

/** One heard text. Compact: clamped to about three lines with "more", opened up
 * from the start when a highlighted word would otherwise be cut off. */
function Heard({ t, highlights, compact }: { t: HeardText; highlights: Highlight[]; compact: boolean }) {
  const text = useRef<HTMLQuoteElement>(null);
  const [open, setOpen] = useState(false);
  const [overflows, setOverflows] = useState(false);
  useLayoutEffect(() => {
    const el = text.current;
    if (!compact || !el) return;
    setOverflows(el.scrollHeight > el.clientHeight + 1);
    const clipped = [...el.querySelectorAll<HTMLElement>("mark")].some((mark) => mark.offsetTop + mark.offsetHeight > el.clientHeight + 1);
    if (clipped) setOpen(true);
  }, [compact, t.text]);
  const spans = highlightSpans(t.text, highlights);
  const parts: (string | JSX.Element)[] = [];
  let at = 0;
  spans.forEach((span, i) => {
    if (span.start > at) parts.push(t.text.slice(at, span.start));
    parts.push(<mark key={i} className={span.used ? "transcript-hit is-used" : "transcript-hit"}
      title={span.used ? `${span.label} (filled in)` : span.label} data-fx={span.ids.join(" ") || undefined}>{t.text.slice(span.start, span.end)}</mark>);
    at = span.end;
  });
  parts.push(t.text.slice(at));
  const clamped = compact && !open;
  return <figure>
    <figcaption className="spot-label">{t.source === "note" ? "WHAT YOU SAID" : "HEARD IN YOUR VIDEO"} · {t.language.toUpperCase()}</figcaption>
    <blockquote ref={text} className={clamped ? "is-clamped" : undefined}>{parts}</blockquote>
    {compact && (overflows || open) && <button type="button" className="link-btn transcript-more" aria-expanded={open} onClick={() => setOpen((v) => !v)}>{open ? "less" : "more"}</button>}
  </figure>;
}

/**
 * What was heard in a voice note or a clip's sound, with the words each open
 * suggestion came from marked (#112). It shows the observer what the model
 * worked from, so a wrong suggestion can be traced to a misheard word.
 */
export default function Transcript({ transcripts, highlights = [], compact = false }: { transcripts: HeardText[]; highlights?: Highlight[]; compact?: boolean }) {
  if (!transcripts.length) return null;
  return <div className="transcript">
    {transcripts.map((t) => <Heard key={t.source} t={t} highlights={highlights} compact={compact} />)}
    {!compact && highlights.length > 0 && <p className="hint">
      Highlighted words are where the suggestions came from.{highlights.some((h) => h.used) && " Dotted ones are already filled in."}
    </p>}
  </div>;
}
