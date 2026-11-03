import { useCallback, useEffect, useRef, useState } from "react";
import { commonSpecies, speciesNoun } from "../species";
import BlockSheet from "../components/BlockSheet";
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

/** What to call the pair: the species if both sides agree, else "animal". */
const pairNoun = (p: Proposal) => speciesNoun(commonSpecies([p.a.species, p.b.species]));

const mergeWarning = (noun: string) =>
  `Mark these as the same ${noun}? This joins both sightings into one animal and can't be undone here.`;

function EvidenceZoom({ url, label, onClose }: { url: string; label: string; onClose: () => void }) {
  const [zoomed, setZoomed] = useState(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    closeRef.current?.focus({ preventScroll: true });
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.stopPropagation(); onClose(); return; }
      if (event.key !== "Tab") return;
      const inside = [...dialogRef.current!.querySelectorAll<HTMLElement>("button, [tabindex='0']")];
      event.preventDefault();
      const at = inside.indexOf(document.activeElement as HTMLElement);
      inside[(at + (event.shiftKey ? -1 : 1) + inside.length) % inside.length]?.focus({ preventScroll: true });
    };
    document.addEventListener("keydown", onKey, true);
    return () => { document.removeEventListener("keydown", onKey, true); opener?.focus?.({ preventScroll: true }); };
  }, [onClose]);
  return <div className="match-zoom" onClick={onClose}>
    <div className="match-zoom-frame" ref={dialogRef} role="dialog" aria-modal="true" aria-label={label}
      onClick={(event) => event.stopPropagation()}>
      <div className="match-zoom-body" tabIndex={0} role="region" aria-label="Photo, scrollable when zoomed">
        <img src={url} alt={label} className={zoomed ? "is-zoomed" : ""} />
      </div>
      <div className="match-zoom-actions">
        <button type="button" className="btn" aria-pressed={zoomed} onClick={() => setZoomed(!zoomed)}>
          {zoomed ? "Fit photo" : "Zoom in"}
        </button>
        <button type="button" className="btn" ref={closeRef} onClick={onClose}>Close photo</button>
      </div>
    </div>
  </div>;
}

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
  // Set while blocking whoever logged one side of a pair (#180).
  const [blocking, setBlocking] = useState<string | null>(null);
  const [zoom, setZoom] = useState<{ url: string; label: string } | null>(null);
  const closeZoom = useCallback(() => setZoom(null), []);

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
    if (verdict === "same" && !confirm(mergeWarning(pairNoun(p)))) return;
    setBusy(p.id);
    try {
      await resolveProposal(p.id, verdict);
      // Drop it from the list rather than refetching: a refetch would reorder
      // the queue under someone working down it.
      setItems((cur) => (cur ?? []).filter((x) => x.id !== p.id));
      setNote(
        verdict === "same"
          ? `Saved as one ${pairNoun(p)}. You'll find them under DOGS.`
          : `Saved as two different ${pairNoun(p)}s.`,
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
      {zoom && <EvidenceZoom {...zoom} onClose={closeZoom} />}
      {blocking && (
        <BlockSheet sightingId={blocking} offerReport={false} onClose={() => setBlocking(null)}
          onBlocked={() => {
            setBlocking(null);
            // Every pair with one of theirs goes; the server knows which.
            getProposals().then((r) => setItems(r.proposals)).catch(() => {});
            setNote("Blocked. Their photos won't come up here again.");
          }}
          onUnauthorized={onUnauthorized} />
      )}
      {back}
      <div className="journal-head">
        {items.length} POSSIBLE MATCH{items.length === 1 ? "" : "ES"} · SAME {speciesNoun(commonSpecies(items.flatMap((p) => [p.a.species, p.b.species]))).toUpperCase()} OR NOT?
      </div>
      {note && <p className="hint">{note}</p>}
      {items.map((p) => (
        <div key={p.id} className="match-card">
          <div className="match-pair">
            {[p.a, p.b].map((side, sideIndex) => (
              <div key={side.sighting_id} className="match-side">
                {side.thumb_url ? (
                  <button type="button" className="match-photo"
                    aria-label={`Enlarge ${sideIndex === 0 ? "new" : "matching"} sighting photo 1`}
                    onClick={() => setZoom({ url: side.photo_url || side.thumb_url!,
                      label: `${sideIndex === 0 ? "New" : "Matching"} sighting photo 1` })}>
                    <img src={side.thumb_url} alt="sighting" />
                  </button>
                ) : (
                  <div className="match-blank">🐾</div>
                )}
                {side.matched && <span className="match-evidence-label">MATCHED PHOTO</span>}
                {side.photos && side.photos.length > 1 && <>
                  <span className="match-evidence-label">{side.photos.length} VIEWS</span>
                  <div className="match-other-views" aria-label="Supporting views of the new sighting">
                    {side.photos.slice(1).map((photo, index) => (
                      <button type="button" className="match-photo" key={photo.photo_id}
                        aria-label={`Enlarge new sighting photo ${index + 2}`}
                        onClick={() => setZoom({ url: photo.photo_url, label: `New sighting photo ${index + 2}` })}>
                        <img src={photo.thumb_url} alt={`Supporting view ${index + 2}`} loading="lazy" />
                      </button>
                    ))}
                  </div>
                </>}
                <span className="match-date">
                  {side.date}
                  {side.mine ? " · YOUR PHOTO" : ""}
                </span>
                {!side.mine && (
                  <button className="link-btn match-block" onClick={() => setBlocking(side.sighting_id)}>block</button>
                )}
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
              DIFFERENT {pairNoun(p).toUpperCase()}S
            </button>
            <button
              className="btn-same"
              disabled={busy === p.id}
              onClick={() => decide(p, "same")}
            >
              SAME {pairNoun(p).toUpperCase()}
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
