import { useState } from "react";
import { blockSighting, UnauthorizedError } from "../api";

export function blockWarning(name?: string | null): string {
  const who = name ? name : "this person";
  return `You won’t see ${who}’s sightings, photos or name any more, and they won’t see yours. They aren’t told. You can unblock them under “blocked people”.`;
}

/**
 * Block whoever logged a sighting (#180). Offers reporting the sighting too,
 * unticked: blocking is about you, reporting is about what everyone sees.
 */
export default function BlockSheet({ sightingId, name, offerReport = true, onClose, onBlocked, onUnauthorized }: {
  sightingId: string;
  name?: string | null;
  /** Off where the sighting cannot be reported from (a match pair). */
  offerReport?: boolean;
  onClose: () => void;
  /** After the block; `report` when they asked to report the sighting too. */
  onBlocked: (report: boolean) => void;
  onUnauthorized: () => void;
}) {
  const [report, setReport] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  async function submit() {
    setBusy(true);
    setFailed(false);
    try {
      await blockSighting(sightingId);
      onBlocked(report);
    } catch (err) {
      if (err instanceof UnauthorizedError) onUnauthorized();
      else setFailed(true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="viewer-overlay" onClick={onClose}>
      <div className="report-sheet" role="dialog" aria-label="Block" onClick={(e) => e.stopPropagation()}>
        <h2>Block {name ?? "this person"}?</h2>
        <p className="hint">{blockWarning(name)}</p>
        {offerReport && (
          <label className="block-report">
            <input type="checkbox" checked={report} onChange={(e) => setReport(e.target.checked)} />
            Also report this sighting
          </label>
        )}
        {failed && <p className="signin-error" role="alert">Couldn’t block just now. Try again.</p>}
        <div className="actions-row">
          <button className="btn btn-secondary" onClick={onClose} disabled={busy}>CANCEL</button>
          <button className="btn btn-primary" onClick={() => void submit()} disabled={busy}>BLOCK</button>
        </div>
      </div>
    </div>
  );
}
