import { useEffect, useState } from "react";
import { banObserver, getHeldQueue, ruleOnUpload, UnauthorizedError, type HeldItem, type HoldReason } from "../api";
import { formatWhen } from "../formatWhen";

/** Uploads screening held back from everyone but their owner (#181).
 *
 * Nothing here is on the shared map, a dog page, a match or a count until a
 * moderator shows it. `SHOW IT` puts it everywhere at once; `KEEP IT HIDDEN`
 * leaves it with its owner. Either can be ruled again later, and neither
 * deletes anything. Oldest first, so nobody's upload waits behind newer ones.
 */

const REASON_LABELS: Record<HoldReason, string> = {
  person: "A PERSON FILLS THE FRAME",
  indoor: "LOOKS INDOORS",
  new_account: "NEW ACCOUNT",
  unscreened: "COULDN'T BE CHECKED",
  not_screened: "NOT CHECKED YET",
  model_person: "AI: A PERSON OR A FACE",
  model_home: "AI: INSIDE A HOME",
  model_sexual: "AI: SEXUAL CONTENT",
  model_graphic: "AI: GRAPHIC",
  model_text: "AI: ABUSIVE TEXT",
};

export default function HeldQueue({ onUnauthorized }: { onUnauthorized: () => void }) {
  const [items, setItems] = useState<HeldItem[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    getHeldQueue()
      .then((r) => setItems(r.items))
      .catch((err) => {
        if (err instanceof UnauthorizedError) onUnauthorized();
        else setFailed(true);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function decide(item: HeldItem, verdict: "approve" | "reject") {
    setBusy(item.capture_id);
    try {
      await ruleOnUpload(item.capture_id, verdict);
      // Dropped, not refetched, so the list does not reorder under someone.
      setItems((cur) => (cur ?? []).filter((x) => x.capture_id !== item.capture_id));
    } catch (err) {
      if (err instanceof UnauthorizedError) onUnauthorized();
      else alert(err instanceof Error ? err.message : "Couldn't save that just now.");
    } finally {
      setBusy(null);
    }
  }

  /** A ban hides everything this person logged, for everyone (#180). */
  async function ban(item: HeldItem) {
    if (!item.observer_id) return;
    const reason = prompt(`Ban ${item.observer ?? "this person"}? Everything they logged is hidden from everyone, and they can't sign in. You can lift it under STATS. Reason (for the record):`);
    if (reason === null) return;
    setBusy(item.capture_id);
    try {
      await banObserver(item.observer_id, reason.trim() || undefined);
      setItems((cur) => (cur ?? []).filter((x) => x.observer_id !== item.observer_id));
    } catch (err) {
      if (err instanceof UnauthorizedError) onUnauthorized();
      else alert(err instanceof Error ? err.message : "Couldn't ban just now.");
    } finally {
      setBusy(null);
    }
  }

  if (failed) return <div className="empty-state">COULDN'T LOAD THE QUEUE — TRY AGAIN</div>;
  if (items === null) return <div className="empty-state">READING HELD UPLOADS…</div>;
  if (items.length === 0) {
    return (
      <div className="empty-state">
        <span className="big">🛡️</span>
        NOTHING HELD —<br />
        UPLOADS SCREENING STOPS APPEAR HERE
      </div>
    );
  }

  return (
    <div className="review">
      <div className="journal-head">
        {items.length} HELD UPLOAD{items.length === 1 ? "" : "S"} · ONLY THEIR OWNERS CAN SEE THEM
      </div>
      {items.map((item) => (
        <div key={item.capture_id} className="match-card">
          <div className="held-thumbs">
            {item.thumb_urls.length > 0 ? (
              item.thumb_urls.map((url, i) => <img key={i} className="mod-thumb" src={url} alt={`held upload, photo ${i + 1}`} />)
            ) : (
              <div className="match-blank">🐾</div>
            )}
          </div>
          <div className="mod-meta">
            <div className="line">
              {formatWhen(item.created_at)}
              {item.kind === "video" && " · CLIP"}
              <br />
              {item.observer ? `logged by ${item.observer}` : "observer unknown"}
            </div>
            <div className="marks">
              {item.reasons.map((r) => (
                <span key={r} className="mk">
                  {REASON_LABELS[r] ?? r}
                </span>
              ))}
            </div>
          </div>
          {(item.note || item.known_names.length > 0) && (
            <ul className="mod-notes">
              {item.note && <li>{item.note}</li>}
              {item.known_names.map((n) => (
                <li key={n}>name: {n}</li>
              ))}
            </ul>
          )}
          <div className="match-actions">
            <button className="btn-different" disabled={busy === item.capture_id} onClick={() => void decide(item, "approve")}>
              SHOW IT
            </button>
            <button className="btn-same" disabled={busy === item.capture_id} onClick={() => void decide(item, "reject")}>
              KEEP IT HIDDEN
            </button>
          </div>
          {item.observer_id && (
            <button className="link-btn" disabled={busy === item.capture_id} onClick={() => void ban(item)}>
              Ban {item.observer ?? "this person"}
            </button>
          )}
        </div>
      ))}
    </div>
  );
}
