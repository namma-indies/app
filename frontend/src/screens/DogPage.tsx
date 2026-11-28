import { useEffect, useState } from "react";
import { getDog, HttpError, UnauthorizedError, type DogPage as Page, type DogPageSighting } from "../api";
import { apart } from "../components/Recognised";
import { dogName, dogNumber, dogTitle } from "../dogLabel";
import { formatDay, formatDayRange, formatWhen } from "../formatWhen";
import { speciesNoun } from "../species";
import { companyHead, TAG_LABELS, whereLine } from "./Dogs";

/** One animal's own page.
 *
 * The card in DOGS is a summary; this is the entry. The latest photograph as
 * the plate, the facts under it, then the first and latest photographs side by
 * side (the then-and-now the recognition card shows once, kept here for good),
 * who they keep company with, and every sighting of them.
 *
 * Other people's sightings are listed with the day and the photograph only.
 * The server already withholds their time and place (routes/dogs.py), so a
 * page about one dog cannot be read as a schedule of where they are when.
 */

function caption(s: DogPageSighting): string {
  if (s.mine && s.captured_at) return [formatWhen(s.captured_at), s.place].filter(Boolean).join(" · ");
  return `${formatDay(`${s.day}T00:00:00`)} · ${s.observer ?? "someone else"}`;
}

function whenOf(s: DogPageSighting): string {
  return s.captured_at ?? `${s.day}T12:00:00`;
}

export default function DogPage({
  id,
  onBack,
  onOpen,
  onUnauthorized,
  faces,
}: {
  id: string;
  /** A face for each animal the list has loaded, for the company row. */
  faces?: Map<string, string | undefined>;
  onBack: () => void;
  /** Open another animal's page (a companion). */
  onOpen: (id: string) => void;
  onUnauthorized: () => void;
}) {
  const [page, setPage] = useState<Page | null>(null);
  const [failed, setFailed] = useState<"missing" | "error" | null>(null);

  useEffect(() => {
    let gone = false;
    setPage(null);
    setFailed(null);
    getDog(id)
      .then((p) => { if (!gone) setPage(p); })
      .catch((err) => {
        if (gone) return;
        if (err instanceof UnauthorizedError) onUnauthorized();
        else setFailed(err instanceof HttpError && err.status === 404 ? "missing" : "error");
      });
    window.scrollTo?.(0, 0);
    return () => { gone = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const back = <button className="link-btn review-back" onClick={onBack}>← All dogs</button>;
  if (failed === "missing") return <div className="empty-state">{back}THIS ANIMAL ISN'T IN YOUR GUIDE</div>;
  if (failed) return <div className="empty-state">{back}COULDN'T LOAD THEM — TRY AGAIN</div>;
  if (page === null) return <div className="empty-state">OPENING THE ENTRY…</div>;

  const pictured = page.sightings.filter((s) => s.thumb_url);
  const latest = pictured[0];
  const first = pictured[pictured.length - 1];
  const thenAndNow = latest && first && latest !== first && latest.day !== first.day;
  const noun = speciesNoun(page.species);

  return (
    <article className="dog-page" aria-label={dogName(page)}>
      {back}
      <h2 className={page.name ? "dog-page-title" : "dog-page-title anon"}>{dogTitle(page)}</h2>
      {page.photos[0] && <img className="dog-lead" src={page.photos[0]} alt={`${dogName(page)}, the latest photograph`} />}

      <div className="dog-meta">
        <div className="line">
          {page.sighting_count} SIGHTING{page.sighting_count === 1 ? "" : "S"} · {formatDayRange(page.first_seen, page.last_seen)}
          <br />
          {whereLine(page)}
        </div>
        {page.observers.length > 0 && <div className="dog-by">also logged by {page.observers.join(", ")}</div>}
        {page.tags.length > 0 && (
          <div className="marks">
            {page.tags.map((t) => <span key={t} className="mk">{TAG_LABELS[t] ?? t.toUpperCase()}</span>)}
          </div>
        )}
      </div>

      {thenAndNow && (
        <section className="dog-section">
          <h3>THEN AND NOW</h3>
          <div className="recognised-pair">
            {[first, latest].map((s, n) => (
              <figure key={n} className="recognised-side">
                <img src={s.thumb_url!} alt="" />
                <figcaption>
                  {formatDay(whenOf(s)).toUpperCase()}
                  <span>{s.mine ? "YOU" : (s.observer ?? "").toUpperCase()}</span>
                </figcaption>
              </figure>
            ))}
          </div>
          <div className="recognised-apart">THE SAME {noun.toUpperCase()} · {apart(whenOf(first), whenOf(latest))}</div>
        </section>
      )}

      {page.company.length > 0 && (
        <section className="dog-section company">
          <div className="company-head">{companyHead(page)}</div>
          <div className="company-row">
            {page.company.map((c) => (
              <button key={c.id} className="companion" onClick={() => onOpen(c.id)} aria-label={`Open ${dogNumber(c.number)}`}>
                {faces?.get(c.id) ? <img src={faces.get(c.id)} alt="" /> : <div className="company-blank">🐾</div>}
                <span className="company-name">{dogNumber(c.number)}</span>
                {c.together > 1 && <span className="company-times">{c.together} TIMES</span>}
              </button>
            ))}
          </div>
        </section>
      )}

      <section className="dog-section">
        <h3>EVERY SIGHTING</h3>
        <div className="dog-sightings">
          {page.sightings.map((s, n) => (
            <figure key={s.id ?? `other-${n}`} className={s.mine ? "dog-sighting mine" : "dog-sighting"}>
              {s.thumb_url ? <img src={s.thumb_url} alt="" /> : <div className="recognised-blank">🐾</div>}
              <figcaption>{caption(s)}</figcaption>
            </figure>
          ))}
        </div>
        {page.sightings.length < page.sighting_count && (
          <p className="hint">The latest {page.sightings.length} of {page.sighting_count}.</p>
        )}
      </section>
    </article>
  );
}
