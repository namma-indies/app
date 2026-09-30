import { useEffect, useState } from "react";
import {
  getModerationQueue,
  reviewSighting,
  UnauthorizedError,
  type ModerationItem,
  type ReportReason,
} from "../api";
import AnimalAudit from "../components/AnimalAudit";

const REASON_LABELS: Record<ReportReason, string> = {
  endangers_dog: "PUTS THE DOG AT RISK",
  offensive: "OFFENSIVE",
  not_a_dog: "NOT A DOG",
  wrong_place: "WRONG PLACE/TIME",
  other: "OTHER",
};

export default function Moderation({ onUnauthorized }: { onUnauthorized: () => void }) {
  const [queue, setQueue] = useState<"reported" | "audit">("reported");
  const [items, setItems] = useState<ModerationItem[] | null>(null);
  const [failedReported, setFailedReported] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    getModerationQueue()
      .then((r) => setItems(r.items))
      .catch((err) => {
        if (err instanceof UnauthorizedError) onUnauthorized();
        else setFailedReported(true);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function decide(item: ModerationItem, verdict: "valid" | "rejected") {
    setBusy(item.sighting_id);
    try {
      await reviewSighting(item.sighting_id, verdict);
      // Preserve the order someone is walking rather than refetching.
      setItems((cur) => (cur ?? []).filter((x) => x.sighting_id !== item.sighting_id));
    } catch (err) {
      if (err instanceof UnauthorizedError) onUnauthorized();
      else setFailedReported(true);
    } finally {
      setBusy(null);
    }
  }

  const toggle = (
    <div className="scope-toggle">
      <button className={queue === "reported" ? "active" : ""} onClick={() => setQueue("reported")}>
        REPORTED
      </button>
      <button className={queue === "audit" ? "active" : ""} onClick={() => setQueue("audit")}>
        DETECTOR AUDIT
      </button>
    </div>
  );

  if (queue === "audit") return <>{toggle}<AnimalAudit onUnauthorized={onUnauthorized} /></>;
  if (failedReported) return <>{toggle}<div className="empty-state">COULDN'T LOAD THE QUEUE — TRY AGAIN</div></>;
  if (items === null) return <>{toggle}<div className="empty-state">READING REPORTS…</div></>;
  if (items.length === 0) {
    return <>{toggle}<div className="empty-state">NOTHING REPORTED —<br />FLAGGED SIGHTINGS APPEAR HERE</div></>;
  }

  return (
    <>
      {toggle}
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
                <div className="match-blank">NO PHOTO</div>
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
                    <span key={r} className="mk">{REASON_LABELS[r] ?? r}</span>
                  ))}
                </div>
              </div>
            </div>
            {item.notes.length > 0 && (
              <ul className="mod-notes">
                {item.notes.map((n, i) => <li key={i}>{n}</li>)}
              </ul>
            )}
            <div className="match-actions">
              <button className="btn-different" disabled={busy === item.sighting_id} onClick={() => decide(item, "valid")}>
                KEEP IT
              </button>
              <button className="btn-same" disabled={busy === item.sighting_id} onClick={() => decide(item, "rejected")}>
                HIDE IT
              </button>
            </div>
          </div>
        ))}
      </div>
    </>
  );
}
