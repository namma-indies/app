import { useEffect, useState } from "react";
import { getDogs, UnauthorizedError, type Dog, type DogsResponse } from "../api";

/** The catalogue of identified individuals.
 *
 * Distinct from the journal on purpose. The journal is a log — every time
 * anyone saw anything, newest first. This is the population: one card per
 * animal, however many times it has been seen. A dog only appears here once a
 * human has confirmed two sightings are the same animal, so the list is the
 * visible output of the re-ID work rather than a second view of the same rows.
 */

const TAG_LABELS: Record<string, string> = {
  male: "♂ MALE",
  female: "♀ FEMALE",
  left: "NOTCH-L",
  right: "NOTCH-R",
  healthy: "HEALTHY",
  injured: "INJURED",
};

function day(iso: string): string {
  return new Date(`${iso}T00:00:00`).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

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
  // Full precision, the same as the map already shows this cohort. When issue
  // #5's area-label coarsening lands it replaces this line, and must land on
  // the map at the same time — a stricter dog card next to a precise map
  // protects nothing.
  if (d.lat == null || d.lng == null) return "NO LOCATION RECORDED";
  return `LAST SEEN ${d.lat.toFixed(4)}, ${d.lng.toFixed(4)}`;
}

export default function Dogs({ onUnauthorized }: { onUnauthorized: () => void }) {
  const [data, setData] = useState<DogsResponse | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    getDogs()
      .then(setData)
      .catch((err) => {
        if (err instanceof UnauthorizedError) onUnauthorized();
        else setFailed(true);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (failed) return <div className="empty-state">COULDN'T LOAD THE DOGS — TRY AGAIN</div>;
  if (data === null) return <div className="empty-state">READING THE CATALOGUE…</div>;

  const dogs = data.dogs;
  const byId = new Map(dogs.map((d) => [d.id, d]));

  if (dogs.length === 0) {
    return (
      <div className="empty-state">
        <span className="big">🐕</span>
        NO DOGS IDENTIFIED YET —<br />
        CONFIRM A MATCH AND THE FIRST ONE APPEARS HERE
      </div>
    );
  }

  return (
    <div className="dogs">
      <div className="journal-head">
        {dogs.length} INDIE{dogs.length === 1 ? "" : "S"} IDENTIFIED
      </div>
      {dogs.map((d, i) => (
        <div key={d.id} className="dog-card">
          <div className="dog-strip">
            {d.photos.map((src, n) => (
              <img key={n} src={src} alt="" />
            ))}
          </div>
          <div className="dog-meta">
            <div className={d.name ? "name" : "name anon"}>
              {d.name ?? `— NO. ${String(dogs.length - i).padStart(3, "0")} —`}
            </div>
            <div className="line">
              {seenLine(d)}
              <br />
              {day(d.first_seen)} → {day(d.last_seen)}
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
            {d.looks_like.length > 0 && (
              /* Worded as a question on purpose. On this population two
                 different dogs that resemble each other score higher than two
                 sightings of the same dog, so presenting these as findings
                 would be presenting a known-wrong answer confidently. The
                 score is shown so a reviewer can calibrate their own eye. */
              <div className="lookalikes">
                <div className="lookalike-head">SAME DOG? — NEEDS A HUMAN</div>
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
