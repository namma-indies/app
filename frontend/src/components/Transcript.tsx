import { useLayoutEffect, useRef, useState } from "react";
import type { LaterNote } from "../api";
import { formatDay } from "../formatWhen";

export interface HeardText {
  source: "note" | "clip";
  language: string;
  text: string;
  /** A translation of a non-English one (#152): shown under it, never highlighted. */
  english?: string | null;
  /** Which transcript, and the language to offer "Redo in" (#169), if any. */
  id?: string | null;
  redo_language?: string | null;
  /** Transcribed again, and not yet read for details (#169). */
  reading?: boolean;
  /** A note added after the upload (#156): which one, when, and its audio. */
  note_id?: string | null;
  added_at?: string | null;
  audio_url?: string | null;
}

/** What a later note's block says while there is no transcript to show, or
 * while the model is still reading the one there is. */
const NOTE_STATUS: Record<LaterNote["state"], string | null> = {
  transcribing: "Listening to your note…",
  reading: "Reading it for details…",
  done: null,
  no_speech: "No speech heard in this note.",
  failed: "Couldn’t transcribe this note.",
};

/** The upload's own note or clip, transcribed again (#169): its old words are
 * gone, so how it ended is said even when no new ones came. */
const RELISTENING = {
  listening: "Listening again…",
  no_speech: "No speech heard this time.",
  failed: "Couldn’t transcribe this note again.",
} as const;

const addedLater = (when: string) => `ADDED LATER · ${formatDay(when).toUpperCase()}`;

export const DELETE_NOTE_WARNING = "Delete this note? Its recording, its words and any suggestions from it you haven’t answered will be removed. Details you’ve already filled in stay.";

/** "delete note" on a note added later (#158): asks first, and says so if it could not. */
function DeleteNote({ onDelete }: { onDelete: () => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  async function remove() {
    if (!confirm(DELETE_NOTE_WARNING)) return;
    setBusy(true);
    setFailed(false);
    try {
      await onDelete();
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  }
  return <>
    <button type="button" className="link-btn transcript-delete" disabled={busy} onClick={remove}>{busy ? "deleting…" : "delete note"}</button>
    {failed && <p className="hint place-warning" role="alert">Couldn’t delete the note. Try again.</p>}
  </>;
}

/** The languages people here speak (app/suggestions.EXPECTED_LANGUAGES), by name. */
const LANGUAGE_NAMES: Record<string, string> = {
  en: "English", hi: "Hindi", kn: "Kannada", ta: "Tamil", te: "Telugu", ml: "Malayalam",
  mr: "Marathi", bn: "Bengali", gu: "Gujarati", pa: "Punjabi", ur: "Urdu",
};

/** The caption's language: a name, or "language unclear" for a code outside
 * that set, which is more likely whisper mishearing than anyone speaking it. */
export function languageLabel(code: string): string {
  return (LANGUAGE_NAMES[code] ?? "Language unclear").toUpperCase();
}

/** "Wrong language? Redo in English" (#169): asks first, as it replaces the words
 * and the suggestions not yet answered, and says so if it could not. */
function RedoLine({ heardAs, language, onRedo }: { heardAs: string; language: string; onRedo: () => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const name = LANGUAGE_NAMES[language] ?? language;
  async function redo() {
    if (!confirm(`Transcribe this note again in ${name}? Suggestions from it you haven’t answered will go; details you’ve filled in stay.`)) return;
    setBusy(true);
    setFailed(false);
    try {
      await onRedo();
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  }
  return <>
    <p className="hint transcript-redo">Heard as {heardAs} · <button type="button" className="link-btn" disabled={busy} onClick={redo}>
      Wrong language? Redo in {name}</button></p>
    {failed && <p className="hint place-warning" role="alert">Couldn’t do that just now. Try again later.</p>}
  </>;
}

/** A word or phrase a suggestion came from, and what it suggested. */
export interface Highlight {
  quote: string;
  label: string;
  /** The suggestion it belongs to, so its chip can find these words (useSuggestionFlow). */
  id?: string;
  /** Already answered and saved: still marked, but quieter than one that still needs you. */
  used?: boolean;
  /** The note added later the quote is from, null for the upload's own note or clip:
   * only that transcript marks it. Left out, any transcript may (an older server). */
  noteId?: string | null;
}

/** The highlights that belong to one transcript: a quote is evidence only where it was said. */
export function highlightsFor(t: HeardText, highlights: Highlight[]): Highlight[] {
  return highlights.filter((h) => h.noteId === undefined || h.noteId === (t.note_id ?? null));
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
function Heard({ t, highlights, compact, state, onDelete, onRedo }: {
  t: HeardText; highlights: Highlight[]; compact: boolean; state?: LaterNote["state"]; onDelete?: () => Promise<void>;
  onRedo?: (id: string, language: string) => Promise<void>;
}) {
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
  const spans = highlightSpans(t.text, highlightsFor(t, highlights));
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
  const known = t.language in LANGUAGE_NAMES;
  const said = t.added_at ? addedLater(t.added_at) : t.source === "note" ? "WHAT YOU SAID" : "HEARD IN YOUR VIDEO";
  return <figure>
    <figcaption className="spot-label">{said} · {languageLabel(t.language)}</figcaption>
    {t.audio_url && <audio className="viewer-audio" src={t.audio_url} controls preload="none" aria-label="voice note added later" />}
    <blockquote ref={text} lang={known ? t.language : undefined} className={clamped ? "is-clamped" : undefined}>{parts}</blockquote>
    {/* Folded away with the rest in the compact sheet; "more" opens both. */}
    {t.english && !clamped && <div className="transcript-english">
      <span className="spot-label">IN ENGLISH</span>
      <p lang="en">{t.english}</p>
    </div>}
    {state && NOTE_STATUS[state] && <p className="hint transcript-status" role="status">{NOTE_STATUS[state]}</p>}
    {!state && t.reading && <p className="hint transcript-status" role="status">{NOTE_STATUS.reading}</p>}
    {t.redo_language && t.id && onRedo && <RedoLine heardAs={languageLabel(t.language)} language={t.redo_language}
      onRedo={() => onRedo(t.id!, t.redo_language!)} />}
    {onDelete && <DeleteNote onDelete={onDelete} />}
    {compact && (overflows || open || !!t.english) && <button type="button" className="link-btn transcript-more" aria-expanded={open} onClick={() => setOpen((v) => !v)}>{open ? "less" : "more"}</button>}
  </figure>;
}

/**
 * What was heard in a voice note or a clip's sound, with the words each open
 * suggestion came from marked (#112). It shows the observer what the model
 * worked from, so a wrong suggestion can be traced to a misheard word.
 */
export default function Transcript({ transcripts, highlights = [], compact = false, notes = [], onDeleteNote, onRedo, relistening = [] }: {
  transcripts: HeardText[]; highlights?: Highlight[]; compact?: boolean;
  /** Notes added later (#156): their state, and a block for each with no transcript yet. */
  notes?: LaterNote[];
  /** Offers "delete note" on each note added later (#158). */
  onDeleteNote?: (noteId: string) => Promise<void>;
  /** Offers "Redo in <language>" where the server says it may (#169). */
  onRedo?: (transcriptId: string, language: string) => Promise<void>;
  /** The upload's own note or clip, being transcribed again (#169). */
  relistening?: { source: "note" | "clip"; state?: "listening" | "failed" | "no_speech" }[];
}) {
  const stateOf = new Map(notes.map((n) => [n.id, n.state]));
  const heard = new Set(transcripts.flatMap((t) => (t.note_id ? [t.note_id] : [])));
  const waiting = notes.filter((n) => !heard.has(n.id) && NOTE_STATUS[n.state]);
  if (!transcripts.length && !waiting.length && !relistening.length) return null;
  return <div className="transcript">
    {transcripts.map((t) => <Heard key={t.note_id ?? t.source} t={t} highlights={highlights} compact={compact}
      state={t.note_id ? stateOf.get(t.note_id) : undefined}
      onDelete={t.note_id && onDeleteNote ? () => onDeleteNote(t.note_id!) : undefined} onRedo={onRedo} />)}
    {relistening.map((r) => <figure key={`again-${r.source}`}>
      <figcaption className="spot-label">{r.source === "note" ? "WHAT YOU SAID" : "HEARD IN YOUR VIDEO"}</figcaption>
      <p className="hint transcript-status" role="status">{RELISTENING[r.state ?? "listening"]}</p>
    </figure>)}
    {waiting.map((n) => <figure key={n.id}>
      <figcaption className="spot-label">{addedLater(n.added_at)}</figcaption>
      <audio className="viewer-audio" src={n.audio_url} controls preload="none" aria-label="voice note added later" />
      <p className="hint transcript-status" role="status">{NOTE_STATUS[n.state]}</p>
      {onDeleteNote && <DeleteNote onDelete={() => onDeleteNote(n.id)} />}
    </figure>)}
    {!compact && highlights.length > 0 && <p className="hint">
      Highlighted words are where the suggestions came from.{highlights.some((h) => h.used) && " Dotted ones are already filled in."}
    </p>}
  </div>;
}
