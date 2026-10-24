import { useEffect, useState } from "react";
import {
  getProposals,
  HttpError,
  resolveProposal,
  UnauthorizedError,
  type Proposal,
} from "../api";

/** Deciding whether two sightings are the same dog.
 *
 * The pipeline embeds every photo, searches for candidates and writes
 * proposals; this is where a person answers them. `POST /proposal/{id}` is the
 * only path that creates an individual, so without this screen the catalogue
 * stays empty however many photos come in.
 *
 * Anyone may answer any pair, including pairs of other people's photos. It
 * used to be only pairs where the viewer logged both sides (issue #29), which
 * left every cross-observer match with nobody allowed to decide it. Pairs with
 * your own photos still come first, because that is where you remember the
 * animal and the street. Someone else's photo shows a thumbnail and a date and
 * nothing about where it was taken (issue #45).
 *
 * Opened from the Dogs tab, which is where the result of a verdict shows up.
 */

const MERGE_WARNING =
  "Mark these as the same dog? This joins both sightings into one animal and can't be undone here.";

export default function Review({
  onUnauthorized,
  onDecided,
  onBack,
}: {
  onUnauthorized: () => void;
  /** Called after each verdict lands, so the caller can refresh the dogs. */
  onDecided?: () => void;
  /** Present when opened from the Dogs tab: renders a way back to it. */
  onBack?: () => void;
}) {
  const [items, setItems] = useState<Proposal[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  // One line about the last thing that happened, so a verdict visibly did
  // something even though the result lands on another screen.
  const [note, setNote] = useState<string | null>(null);

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
      setNote(
        verdict === "same"
          ? "Saved as one dog. You'll find them in the dogs list."
          : "Saved as two different dogs.",
      );
      onDecided?.();
    } catch (err) {
      if (err instanceof UnauthorizedError) onUnauthorized();
      else if (err instanceof HttpError && err.status === 409) {
        // Everyone works one queue now, so someone else answering this pair
        // first is ordinary, not a failure. Drop it and carry on.
        setItems((cur) => (cur ?? []).filter((x) => x.id !== p.id));
        // 409 also covers a photo reported since the list loaded, or an
        // identity conflict; none of those is answerable from here either.
        setNote("That pair can't be saved now. Someone may have answered it first.");
        onDecided?.();
      } else setFailed(true);
    } finally {
      setBusy(null);
    }
  }

  const back = onBack ? (
    <button className="link-btn review-back" onClick={onBack}>
      ← Back to dogs
    </button>
  ) : null;

  if (failed) return <div className="empty-state">{back}COULDN'T LOAD MATCHES — TRY AGAIN</div>;
  if (items === null) return <div className="empty-state">LOOKING FOR MATCHES…</div>;

  if (items.length === 0) {
    return (
      <div className="review">
        {back}
        {note && <p className="hint">{note}</p>}
        <div className="empty-state">
          <span className="big">🔍</span>
          NOTHING TO REVIEW —<br />
          MATCHES APPEAR WHEN TWO SIGHTINGS LOOK ALIKE
        </div>
      </div>
    );
  }

  return (
    <div className="review">
      {back}
      <div className="journal-head">
        {items.length} POSSIBLE MATCH{items.length === 1 ? "" : "ES"} · SAME DOG OR NOT?
      </div>
      {note && <p className="hint">{note}</p>}
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
                <span className="match-date">
                  {side.date}
                  {side.mine ? " · YOUR PHOTO" : ""}
                </span>
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
