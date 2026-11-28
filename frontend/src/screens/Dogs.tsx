import { useCallback, useEffect, useRef, useState } from "react";
import { getDogs, getProposals, UnauthorizedError, type Dog, type DogsResponse } from "../api";
import Review from "./Review";
import { speciesNoun } from "../species";
import { formatDayRange } from "../formatWhen";
import { dogNumber, dogTitle } from "../dogLabel";

/** The catalogue of identified individuals, and the matches that feed it.
 *
 * Distinct from the journal on purpose. The journal is a log — every time
 * anyone saw anything, newest first. This is the population: one card per
 * animal, however many times they have been seen. A dog only appears here once
 * a human has confirmed two sightings are the same animal, so the list is the
 * visible output of the re-ID work rather than a second view of the same rows.
 *
 * The matches waiting for that confirmation live here too, behind one button
 * at the top, rather than in a tab of their own. A verdict is what makes or
 * grows a dog, so the place you give it is the place you see the result.
 */

const TAG_LABELS: Record<string, string> = {
  male: "♂ MALE",
  female: "♀ FEMALE",
  left: "NOTCH-L",
  right: "NOTCH-R",
  healthy: "HEALTHY",
  injured: "INJURED",
};

function seenLine(d: Dog): string {
  return `${d.sighting_count} SIGHTING${d.sighting_count === 1 ? "" : "S"}`;
}

/** Who logged it, other than you.
 *
 * The map names the observer in a sighting's popup and never on the pin, so
 * attribution belongs on the detail surface -- which is what a dog card is.
 * Same phrasing as the popup's "logged by X" so the two read as one system.
 * Names are typed by observers at /join; React escapes them on render, which
 * is why this returns a string rather than building markup. */
function byLine(d: Dog): string {
  if (d.observers.length === 0) return "";
  if (d.observers.length <= 3) return `logged by ${d.observers.join(", ")}`;
  return `logged by ${d.observers.slice(0, 3).join(", ")} +${d.observers.length - 3} more`;
}

function whereLine(d: Dog): string {
  // Exact only for a dog you have photographed yourself; otherwise the server
  // has already collapsed this to a grid cell, and the card has to say so.
  // Printing four decimals of a coordinate that is accurate to a kilometre
  // invites the reader to believe the digits.
  if (d.lat == null || d.lng == null) return "NO LOCATION RECORDED";
  // In words when the server has a name. "Around" for someone else's
  // sighting: the name is the cell's, and the dog was somewhere in it.
  if (d.place) return `LAST SEEN ${d.precision === "area" ? "AROUND" : "IN"} ${d.place.toUpperCase()}`;
  if (d.precision === "area") {
    const km = d.cell_m ? (d.cell_m / 1000).toFixed(d.cell_m % 1000 === 0 ? 0 : 1) : "1";
    return `LAST SEEN IN A ~${km} KM AREA · ${d.lat.toFixed(2)}, ${d.lng.toFixed(2)}`;
  }
  return `LAST SEEN ${d.lat.toFixed(4)}, ${d.lng.toFixed(4)}`;
}

/** How the row of companions is introduced. "Often" is a claim about a habit,
 * so it waits for a second upload: once is a meeting, twice is company. */
export function companyHead(d: Dog): string {
  return d.company.some((c) => c.together > 1) ? "OFTEN IN THE COMPANY OF" : "SEEN IN THE COMPANY OF";
}

export default function Dogs({
  onUnauthorized,
  focusId,
}: {
  onUnauthorized: () => void;
  /** Open on this animal's card: scrolled to and marked. Set when someone
   * arrives from a mention of them elsewhere (a recognition, the Journal). */
  focusId?: string | null;
}) {
  const [data, setData] = useState<DogsResponse | null>(null);
  // Which card is marked. Starts as the one asked for and follows taps on a
  // companion, so "who is that?" is answered by going to them.
  const [marked, setMarked] = useState<string | null>(focusId ?? null);
  const cards = useRef(new Map<string, HTMLDivElement>());
  const [failed, setFailed] = useState(false);
  // How many pairs are waiting. A failed count only hides the button: the
  // dogs themselves loaded, and the list is still worth showing.
  const [waiting, setWaiting] = useState(0);
  const [reviewing, setReviewing] = useState(false);

  const loadDogs = useCallback(() => {
    getDogs()
      .then((d) => {
        setData(d);
        setFailed(false);
      })
      .catch((err) => {
        if (err instanceof UnauthorizedError) onUnauthorized();
        else setFailed(true);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadWaiting = useCallback(() => {
    getProposals()
      .then((r) => setWaiting(r.proposals.length))
      .catch((err) => {
        if (err instanceof UnauthorizedError) onUnauthorized();
        else setWaiting(0);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    loadDogs();
    loadWaiting();
  }, [loadDogs, loadWaiting]);

  useEffect(() => setMarked(focusId ?? null), [focusId]);
  // After the list is on screen, not before: there is nothing to scroll to
  // while it is still loading.
  const loaded = data !== null;
  useEffect(() => {
    if (!marked || !loaded || reviewing) return;
    cards.current.get(marked)?.scrollIntoView?.({ block: "start", behavior: "smooth" });
  }, [marked, loaded, reviewing]);

  if (reviewing) {
    return (
      <Review
        onUnauthorized={onUnauthorized}
        // Refetch after every verdict, so a new or grown dog is already in the
        // list when the person goes back to it.
        onDecided={loadDogs}
        onBack={() => {
          setReviewing(false);
          loadDogs();
          loadWaiting();
        }}
      />
    );
  }

  const matchesButton =
    waiting > 0 ? (
      <button className="matches-waiting" onClick={() => setReviewing(true)}>
        <span className="matches-count">
          {waiting} MATCH{waiting === 1 ? "" : "ES"} WAITING
        </span>
        <span className="matches-sub">Two photos that might be one animal. Take a look.</span>
      </button>
    ) : null;

  if (failed) return <div className="empty-state">COULDN'T LOAD THE ANIMALS — TRY AGAIN</div>;
  if (data === null) return <div className="empty-state">READING THE CATALOGUE…</div>;

  const dogs = data.dogs;
  const byId = new Map(dogs.map((d) => [d.id, d]));

  if (dogs.length === 0) {
    return (
      <div className="dogs">
        {matchesButton}
        <div className="empty-state">
          <span className="big">🐾</span>
          NO ANIMALS IDENTIFIED YET —<br />
          CONFIRM A MATCH AND THE FIRST ONE APPEARS HERE
        </div>
      </div>
    );
  }

  return (
    <div className="dogs">
      {matchesButton}
      <div className="journal-head">
        {dogs.length} INDIE{dogs.length === 1 ? "" : "S"} IDENTIFIED
      </div>
      {dogs.map((d) => (
        <div
          key={d.id}
          className={d.id === marked ? "dog-card dog-card-marked" : "dog-card"}
          ref={(el) => { if (el) cards.current.set(d.id, el); else cards.current.delete(d.id); }}
        >
          {/* The latest photograph leads, at the card's full width: on a
              field-guide page the animal is the plate, and the facts are the
              caption. The rest are a strip under it. */}
          {d.photos[0] && <img className="dog-lead" src={d.photos[0]} alt="" />}
          {d.photos.length > 1 && (
            <div className="dog-strip">
              {d.photos.slice(1).map((src, n) => (
                <img key={n} src={src} alt="" />
              ))}
            </div>
          )}
          <div className="dog-meta">
            <div className={d.name ? "name" : "name anon"}>
              {dogTitle(d)}
            </div>
            <div className="line">
              {seenLine(d)}
              <br />
              {formatDayRange(d.first_seen, d.last_seen)}
              <br />
              {whereLine(d)}
            </div>
            {byLine(d) && <div className="dog-by">{byLine(d)}</div>}
            {d.tags.length > 0 && (
              <div className="marks">
                {d.tags.map((t) => (
                  <span key={t} className="mk">
                    {TAG_LABELS[t] ?? t.toUpperCase()}
                  </span>
                ))}
              </div>
            )}
            {d.company.length > 0 && (
              /* Stated, where the look-alikes below are asked: these two were
                 in one photograph, which is a fact about an upload and not a
                 model's opinion. So it sits above them and looks like part of
                 the card rather than a queue. A companion beyond the list's
                 cap, or hidden from this viewer, still gets their number. */
              <div className="company">
                <div className="company-head">{companyHead(d)}</div>
                <div className="company-row">
                  {d.company.map((c) => {
                    const other = byId.get(c.id);
                    const label = other?.name ?? dogNumber(c.number);
                    const face = other?.photos[0] ? <img src={other.photos[0]} alt="" /> : <div className="company-blank">🐾</div>;
                    const caption = (
                      <>
                        <span className="company-name">{label}</span>
                        {c.together > 1 && <span className="company-times">{c.together} TIMES</span>}
                      </>
                    );
                    return other ? (
                      <button key={c.id} className="companion" onClick={() => setMarked(c.id)}
                        aria-label={`Go to ${label}`}>
                        {face}{caption}
                      </button>
                    ) : (
                      <div key={c.id} className="companion">{face}{caption}</div>
                    );
                  })}
                </div>
              </div>
            )}
            {d.looks_like.length > 0 && (
              /* Worded as a question on purpose. On this population two
                 different dogs that resemble each other score higher than two
                 sightings of the same dog, so presenting these as findings
                 would be presenting a known-wrong answer confidently. The
                 score is shown so a reviewer can calibrate their own eye. */
              <div className="lookalikes">
                <div className="lookalike-head">SAME {speciesNoun(d.species).toUpperCase()}? — NEEDS A HUMAN</div>
                <div className="lookalike-row">
                  {d.looks_like.map((l) => {
                    const other = byId.get(l.id);
                    return (
                      <div key={l.id} className="lookalike">
                        {other?.photos[0] ? (
                          <img src={other.photos[0]} alt="" />
                        ) : (
                          <div className="lookalike-blank">🐾</div>
                        )}
                        <span
                          className={
                            l.similarity >= data.propose_min ? "score score-high" : "score"
                          }
                        >
                          {l.similarity.toFixed(2)}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
