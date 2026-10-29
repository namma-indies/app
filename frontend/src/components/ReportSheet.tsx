import { useState } from "react";
import {
  MAX_REPORT_NOTE,
  REPORT_REASONS,
  reportReasonLabel,
  reportSighting,
  UnauthorizedError,
  type ReportReason,
} from "../api";

/** Flagging someone else's sighting.
 *
 * The first reason is "puts this animal at risk" (cat or dog, as it is), and the order is deliberate.
 * Most report flows are built for offence; this app's first concern is that a
 * photograph can show where a specific animal sleeps, which is a safety problem
 * rather than a taste one.
 *
 * The sighting comes down as soon as this is sent, and the copy says so. A
 * report form that thanks you and appears to do nothing teaches people the
 * button is decorative, and then nobody uses it for the case that matters.
 */
export default function ReportSheet({
  sightingId,
  species,
  onClose,
  onReported,
  onUnauthorized,
}: {
  sightingId: string;
  /** Of the sighting being reported; the animal reasons name it. */
  species?: string | null;
  onClose: () => void;
  onReported: () => void;
  onUnauthorized: () => void;
}) {
  const [reason, setReason] = useState<ReportReason>("endangers_dog");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      await reportSighting(sightingId, reason, note.trim() || undefined);
      onReported();
    } catch (err) {
      if (err instanceof UnauthorizedError) onUnauthorized();
      else setError("Couldn't send that. Try again in a moment.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="viewer-overlay" onClick={onClose}>
      <div className="report-sheet" onClick={(e) => e.stopPropagation()}>
        <h2>Report this sighting</h2>
        {/* Accurate, not reassuring: hiding now takes a second person agreeing,
            so promising instant removal would be a promise we break. Wording is
            Akash's call -- the constraint is only that it stay true. */}
        <p className="hint">
          One of the team looks at it. If someone else reports it too, it comes
          off the shared map while that happens.
        </p>

        <div className="report-reasons" role="radiogroup" aria-label="Reason">
          {REPORT_REASONS.map((r) => (
            <button
              key={r}
              type="button"
              role="radio"
              aria-checked={reason === r}
              className={"chip" + (reason === r ? " active" : "")}
              onClick={() => setReason(r)}
            >
              {reportReasonLabel(r, species)}
            </button>
          ))}
        </div>

        <label className="report-note-label" htmlFor="report-note">
          Anything we should know? (optional)
        </label>
        <textarea
          id="report-note"
          rows={3}
          maxLength={MAX_REPORT_NOTE}
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="e.g. this shows the gate she sleeps behind"
        />

        {error && (
          <p className="signin-error" role="alert">
            {error}
          </p>
        )}

        <div className="actions-row">
          <button className="btn btn-secondary" onClick={onClose} disabled={busy}>
            CANCEL
          </button>
          <button className="btn btn-primary" onClick={submit} disabled={busy}>
            {busy ? <span className="spinner" /> : "REPORT"}
          </button>
        </div>
      </div>
    </div>
  );
}
