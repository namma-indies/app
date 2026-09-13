import { useEffect, useState } from "react";
import {
  getProposals,
  resolveProposal,
  UnauthorizedError,
  type Proposal,
} from "../api";

/** Deciding whether two of your sightings are the same dog.
 *
 * This is the step that has been missing: the pipeline embeds every photo,
 * searches for candidates and writes proposals, and until now nothing ever
 * showed one to a person. `POST /proposal/{id}` is the only path that creates
 * an individual, so without this screen the catalogue stays empty however many
 * photos come in.
 *
 * Only pairs where the viewer logged both sides appear — see issue #29. That
 * is where the judgement is sound: you remember the animal and the street, and
 * a stranger has only the pixels. MiewID's own numbers say pixels are not
 * enough on their own, since two different dogs that resemble each other can
 * outscore two sightings of the same one.
 */

const MERGE_WARNING =
  "Mark these as the same dog? This joins both sightings into one animal and can't be undone here.";

export default function Review({ onUnauthorized }: { onUnauthorized: () => void }) {
  const [items, setItems] = useState<Proposal[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    getProposals()
      .then((r) => setItems(r.proposals))
      .catch((err) => {
        if (err instanceof UnauthorizedError) onUnauthorized();
        else setFailed(true);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function decide(p: Proposal, verdict: "same" | "different") {
    // Confirm only the merge. "Different" is cheap to get wrong -- the pair can
    // resurface -- while "same" fuses two identities and has no undo in the app.
    if (verdict === "same" && !confirm(MERGE_WARNING)) return;
    setBusy(p.id);
    try {
      await resolveProposal(p.id, verdict);
      // Drop it from the list rather than refetching: a refetch would reorder
      // the queue under someone working down it.
      setItems((cur) => (cur ?? []).filter((x) => x.id !== p.id));
    } catch (err) {
      if (err instanceof UnauthorizedError) onUnauthorized();
      else setFailed(true);
    } finally {
      setBusy(null);
    }
  }

  if (failed) return <div className="empty-state">COULDN'T LOAD MATCHES — TRY AGAIN</div>;
  if (items === null) return <div className="empty-state">LOOKING FOR MATCHES…</div>;

  if (items.length === 0) {
    return (
      <div className="empty-state">
        <span className="big">🔍</span>
        NOTHING TO REVIEW —<br />
        MATCHES APPEAR WHEN TWO OF YOUR SIGHTINGS LOOK ALIKE
      </div>
    );
  }

  return (
    <div className="review">
      <div className="journal-head">
        {items.length} POSSIBLE MATCH{items.length === 1 ? "" : "ES"}
      </div>
      {items.map((p) => (
        <div key={p.id} className="match-card">
          <div className="match-pair">
            {[p.a, p.b].map((side) => (
              <div key={side.sighting_id} className="match-side">
                {side.thumb_url ? (
                  <img src={side.thumb_url} alt="sighting" />
                ) : (
                  <div className="match-blank">🐾</div>
                )}
                <span className="match-date">{side.date}</span>
              </div>
            ))}
          </div>
          {/* The score is context for the reviewer's own eye, not a
              recommendation. Deliberately not phrased as a confidence: on this
              population look-alikes outscore genuine matches. */}
          <div className="match-score">SIMILARITY {p.score.toFixed(2)}</div>
          <div className="match-actions">
            <button
              className="btn-different"
              disabled={busy === p.id}
              onClick={() => decide(p, "different")}
            >
              DIFFERENT DOGS
            </button>
            <button
              className="btn-same"
              disabled={busy === p.id}
              onClick={() => decide(p, "same")}
            >
              SAME DOG
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
