import { useEffect, useState } from "react";
import {
  getModerationQueue,
  reviewSighting,
  UnauthorizedError,
  type ModerationItem,
  type ReportReason,
} from "../api";

/** The queue of reported sightings, for whoever holds `trust_tier='moderator'`.
 *
 * This is the half of a report mechanism that usually does not get built. A
 * report button with nothing behind it satisfies a store reviewer reading a
 * screenshot and satisfies nobody standing in front of a dog that should not be
 * on a public map.
 *
 * Two verdicts, deliberately no third. `KEEP` puts the sighting back and makes
 * that decision sticky, so a later report records itself and surfaces here
 * again without silently taking the photo down. `HIDE` takes it off every
 * shared surface and stops it seeding identities in re-ID. Neither deletes
 * anything: the photograph is evidence of something that happened, hiding is
 * reversible, deletion is not.
 */

const REASON_LABELS: Record<ReportReason, string> = {
  endangers_dog: "PUTS THE DOG AT RISK",
  offensive: "OFFENSIVE",
  not_a_dog: "NOT A DOG",
  wrong_place: "WRONG PLACE/TIME",
  other: "OTHER",
};

export default function Moderation({ onUnauthorized }: { onUnauthorized: () => void }) {
  const [items, setItems] = useState<ModerationItem[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    getModerationQueue()
      .then((r) => setItems(r.items))
      .catch((err) => {
        if (err instanceof UnauthorizedError) onUnauthorized();
        else setFailed(true);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function decide(item: ModerationItem, verdict: "valid" | "rejected") {
    setBusy(item.sighting_id);
    try {
      await reviewSighting(item.sighting_id, verdict);
      // Drop it rather than refetching, so the queue does not reorder under
      // someone working down it.
      setItems((cur) => (cur ?? []).filter((x) => x.sighting_id !== item.sighting_id));
    } catch (err) {
      if (err instanceof UnauthorizedError) onUnauthorized();
      else setFailed(true);
    } finally {
      setBusy(null);
    }
  }

  if (failed) return <div className="empty-state">COULDN'T LOAD THE QUEUE — TRY AGAIN</div>;
  if (items === null) return <div className="empty-state">READING REPORTS…</div>;

  if (items.length === 0) {
    return (
      <div className="empty-state">
        <span className="big">🛡️</span>
        NOTHING REPORTED —<br />
        FLAGGED SIGHTINGS APPEAR HERE
      </div>
    );
  }

  return (
    <div className="review">
      <div className="journal-head">
        {items.length} REPORTED SIGHTING{items.length === 1 ? "" : "S"}
      </div>
      {items.map((item) => (
        <div key={item.sighting_id} className="match-card">
          <div className="mod-head">
            {item.thumb_url ? (
              <img className="mod-thumb" src={item.thumb_url} alt="reported sighting" />
            ) : (
              <div className="match-blank">🐾</div>
            )}
            <div className="mod-meta">
              <div className="line">
                {new Date(item.captured_at).toLocaleString()}
                <br />
                {item.observer ? `logged by ${item.observer}` : "observer unknown"}
                <br />
                {item.report_count} REPORT{item.report_count === 1 ? "" : "S"}
                {item.review_status === "valid" && " · REPORTED AGAIN AFTER REVIEW"}
              </div>
              <div className="marks">
                {[...new Set(item.reasons)].map((r) => (
                  <span key={r} className="mk">
                    {REASON_LABELS[r] ?? r}
                  </span>
                ))}
              </div>
            </div>
          </div>

          {item.notes.length > 0 && (
            <ul className="mod-notes">
              {item.notes.map((n, i) => (
                <li key={i}>{n}</li>
              ))}
            </ul>
          )}

          <div className="match-actions">
            <button
              className="btn-different"
              disabled={busy === item.sighting_id}
              onClick={() => decide(item, "valid")}
            >
              KEEP IT
            </button>
            <button
              className="btn-same"
              disabled={busy === item.sighting_id}
              onClick={() => decide(item, "rejected")}
            >
              HIDE IT
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
