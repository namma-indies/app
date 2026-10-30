import { useState } from "react";
import { decideDescription, UnauthorizedError, type PhotoDescription as Described } from "../api";
import { highlightSpans, type Highlight } from "./Transcript";

const MAX_CHARS = 500;
type Written = Extract<Described, { text: string }>;
const LABEL: Record<Written["status"], string> = {
  open: "AI · SUGGESTED",
  accepted: "AI-WRITTEN",
  edited: "EDITED BY YOU",
};

/** What the Journal may use as the photo's alt text: a description only once
 * its observer has used or edited it, never one still only suggested (#160). */
export function describedAlt(described: Described | null | undefined): string | null {
  return described && (described.status === "accepted" || described.status === "edited") ? described.text : null;
}

/**
 * An AI-written line about the animal, for a sighting with nothing noted (#160).
 *
 * Always labelled as AI's, and only ever a suggestion: "Use" keeps it as
 * written, "Edit" makes the words the observer's own, "Dismiss" hides it for
 * good. It is never folded into the typed note. Words a photo suggestion came
 * from are marked, as a voice note's are.
 */
export default function PhotoDescription({ described, highlights = [], onChanged, onUnauthorized }: {
  described: Written;
  highlights?: Highlight[];
  onChanged: () => void;
  onUnauthorized: () => void;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  async function decide(decision: Parameters<typeof decideDescription>[1]) {
    setBusy(true);
    setFailed(false);
    try {
      await decideDescription(described.id, decision);
      setEditing(null);
      onChanged();
    } catch (err) {
      if (err instanceof UnauthorizedError) onUnauthorized();
      else setFailed(true);
    } finally {
      setBusy(false);
    }
  }

  const spans = highlightSpans(described.text, highlights);
  const parts: (string | JSX.Element)[] = [];
  let at = 0;
  spans.forEach((span, i) => {
    if (span.start > at) parts.push(described.text.slice(at, span.start));
    parts.push(<mark key={i} className={span.used ? "transcript-hit is-used" : "transcript-hit"} title={span.label}
      data-fx={span.ids.join(" ") || undefined}>{described.text.slice(span.start, span.end)}</mark>);
    at = span.end;
  });
  parts.push(described.text.slice(at));

  return <figure className="photo-description">
    <figcaption className="spot-label">{LABEL[described.status]}</figcaption>
    {editing === null ? <>
      <p className={described.status === "open" ? "is-suggested" : undefined}>{parts}</p>
      <div className="photo-description-actions">
        {described.status === "open" && <button type="button" className="btn" disabled={busy} onClick={() => decide({ verdict: "use" })}>Use</button>}
        <button type="button" className="link-btn" disabled={busy} onClick={() => setEditing(described.text)}>Edit</button>
        <button type="button" className="link-btn" disabled={busy} onClick={() => decide({ verdict: "dismiss" })}>
          {described.status === "open" ? "Dismiss" : "Remove"}
        </button>
      </div>
    </> : <>
      <label className="sr-only" htmlFor={`describe-${described.id}`}>Describe the animal</label>
      <textarea id={`describe-${described.id}`} value={editing} maxLength={MAX_CHARS} rows={3} disabled={busy}
        onChange={(event) => setEditing(event.target.value)} />
      <div className="photo-description-actions">
        <button type="button" className="btn" disabled={busy || !editing.trim()} onClick={() => decide({ verdict: "edit", text: editing })}>Save</button>
        <button type="button" className="link-btn" disabled={busy} onClick={() => setEditing(null)}>Cancel</button>
      </div>
    </>}
    {failed && <p className="hint place-warning" role="alert">Couldn’t save that. Try again.</p>}
  </figure>;
}
