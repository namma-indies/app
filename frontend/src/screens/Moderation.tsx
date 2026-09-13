import { useEffect, useState } from "react";
import {
  getFlaggedQueue,
  getModerationQueue,
  reviewSighting,
  ruleOnAnimal,
  UnauthorizedError,
  type FlaggedItem,
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
 *
 * A second, independent queue lives behind the toggle below: sightings the
 * animal detector itself is unsure about, ordered least animal-like first.
 * It is not only a safety net over the hiding threshold — it is how the
 * threshold gets chosen. A histogram says where scores cluster; it cannot say
 * where the detector starts being wrong. Walking this list from the bottom
 * up does: the moderator stops being able to say "no animal" at some point,
 * and that point is the threshold. The list order is therefore load-bearing
 * -- never re-sort it client-side. An animal verdict here never touches
 * `review_status`: being reported and having no animal in the frame at all
 * are different questions, decided independently.
 */

const REASON_LABELS: Record<ReportReason, string> = {
  endangers_dog: "PUTS THE DOG AT RISK",
  offensive: "OFFENSIVE",
  not_a_dog: "NOT A DOG",
  wrong_place: "WRONG PLACE/TIME",
  other: "OTHER",
};

export default function Moderation({ onUnauthorized }: { onUnauthorized: () => void }) {
  const [queue, setQueue] = useState<"reported" | "flagged">("reported");
  const [items, setItems] = useState<ModerationItem[] | null>(null);
  const [flaggedItems, setFlaggedItems] = useState<FlaggedItem[] | null>(null);
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

  useEffect(() => {
    if (queue !== "flagged" || flaggedItems !== null) return;
    getFlaggedQueue()
      .then((r) => setFlaggedItems(r.items))
      .catch((err) => {
        if (err instanceof UnauthorizedError) onUnauthorized();
        else setFailed(true);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queue]);

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

  async function decideAnimal(item: FlaggedItem, verdict: "animal" | "no_animal") {
    setBusy(item.sighting_id);
    try {
      await ruleOnAnimal(item.sighting_id, verdict);
      // Same reasoning as the reported queue: drop locally, don't refetch, so
      // the order someone is walking does not shift under them.
      setFlaggedItems((cur) => (cur ?? []).filter((x) => x.sighting_id !== item.sighting_id));
    } catch (err) {
      if (err instanceof UnauthorizedError) onUnauthorized();
      else setFailed(true);
    } finally {
      setBusy(null);
    }
  }

  if (failed) return <div className="empty-state">COULDN'T LOAD THE QUEUE — TRY AGAIN</div>;

  const toggle = (
    <div className="scope-toggle">
      <button className={queue === "reported" ? "active" : ""} onClick={() => setQueue("reported")}>
        REPORTED
      </button>
      <button className={queue === "flagged" ? "active" : ""} onClick={() => setQueue("flagged")}>
        NOT ANIMALS
      </button>
    </div>
  );

  if (queue === "flagged") {
    if (flaggedItems === null) {
      return (
        <>
          {toggle}
          <div className="empty-state">READING THE DETECTOR'S DOUBTS…</div>
        </>
      );
    }

    if (flaggedItems.length === 0) {
      return (
        <>
          {toggle}
          <div className="empty-state">
            <span className="big">🐾</span>
            NOTHING FLAGGED —<br />
            RUN THE RESCORE FIRST
          </div>
        </>
      );
    }

    return (
      <>
        {toggle}
        <div className="review">
          <div className="journal-head">
            {flaggedItems.length} FLAGGED SIGHTING{flaggedItems.length === 1 ? "" : "S"}
          </div>
          {flaggedItems.map((item) => (
            <div key={item.sighting_id} className="match-card">
              <div className="mod-head">
                {item.thumb_url ? (
                  <img className="mod-thumb" src={item.thumb_url} alt="flagged sighting" />
                ) : (
                  <div className="match-blank">🐾</div>
                )}
                <div className="mod-meta">
                  <div className="line">
                    {new Date(item.captured_at).toLocaleString()}
                    <br />
                    {item.observer ? `logged by ${item.observer}` : "observer unknown"}
                    <br />
                    DOG {item.dog?.toFixed(2) ?? "—"} · CAT {item.cat?.toFixed(2) ?? "—"}
                  </div>
                </div>
              </div>

              <div className="match-actions">
                <button
                  className="btn-different"
                  disabled={busy === item.sighting_id}
                  onClick={() => decideAnimal(item, "animal")}
                >
                  THERE IS AN ANIMAL
                </button>
                <button
                  className="btn-same"
                  disabled={busy === item.sighting_id}
                  onClick={() => decideAnimal(item, "no_animal")}
                >
                  NO ANIMAL
                </button>
              </div>
            </div>
          ))}
        </div>
      </>
    );
  }

  if (items === null) {
    return (
      <>
        {toggle}
        <div className="empty-state">READING REPORTS…</div>
      </>
    );
  }

  if (items.length === 0) {
    return (
      <>
        {toggle}
        <div className="empty-state">
          <span className="big">🛡️</span>
          NOTHING REPORTED —<br />
          FLAGGED SIGHTINGS APPEAR HERE
        </div>
      </>
    );
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
    </>
  );
}
