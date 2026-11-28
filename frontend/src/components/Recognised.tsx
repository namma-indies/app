import { useEffect, useRef, useState } from "react";
import { getRecognised, markRecognisedSeen, UnauthorizedError, type Recognition } from "../api";
import { dogName, dogTitle } from "../dogLabel";
import { formatDay } from "../formatWhen";
import { haptic } from "../haptics";
import { speciesNoun } from "../species";

/** The moment the app says who a sighting of yours turned out to be.
 *
 * Every sighting starts as nobody in particular. Some time later -- an hour, a
 * season -- a person looks at two photographs and says they are one animal,
 * and the sighting has an identity. That used to happen silently: the only way
 * to find out was to scroll DOGS and notice a card. This card says it, with
 * the two photographs that were judged to be the same animal side by side and
 * how far apart in time they were taken.
 *
 * One at a time, oldest first, so that reading one marks exactly that much as
 * read: the server keeps a single "read up to here" time per person, and the
 * order is what lets one timestamp stand for "this card and everything before
 * it" without a table of read receipts.
 *
 * It says nothing about where. The other photograph is one the animal's card
 * already shows this person.
 */

const DAY = 86_400_000;

/** How far apart two sightings are, in the largest unit that reads naturally. */
export function apart(a: string, b: string): string {
  const days = Math.round(Math.abs(+new Date(a) - +new Date(b)) / DAY);
  const many = (n: number, unit: string) => `${n} ${unit}${n === 1 ? "" : "S"} APART`;
  if (days === 0) return "THE SAME DAY";
  if (days < 14) return many(days, "DAY");
  if (days < 60) return many(Math.round(days / 7), "WEEK");
  if (days < 730) return many(Math.round(days / 30.44), "MONTH");
  return many(Math.round(days / 365.25), "YEAR");
}

/** What is known about them beyond this pair: how often you have met, and who
 * else has. Names are typed by people at /join; React escapes them on render. */
export function knownLine(r: Recognition): string {
  const parts: string[] = [];
  if (r.met_count >= 2) parts.push(`You have met them ${r.met_count} times.`);
  const others = r.observers;
  if (others.length === 1) parts.push(`${others[0]} knows them too.`);
  else if (others.length === 2) parts.push(`${others[0]} and ${others[1]} know them too.`);
  else if (others.length > 2) parts.push(`${others.length} other people know them too.`);
  return parts.join(" ");
}

function Side({ src, when, who }: { src: string | null; when: string; who: string }) {
  return (
    <figure className="recognised-side">
      {src ? <img src={src} alt="" /> : <div className="recognised-blank">🐾</div>}
      <figcaption>
        {formatDay(when).toUpperCase()}
        <span>{who}</span>
      </figcaption>
    </figure>
  );
}

export default function Recognised({
  onSee,
  onUnauthorized,
}: {
  /** Open this animal's card under DOGS. */
  onSee: (individualId: string) => void;
  onUnauthorized: () => void;
}) {
  // Oldest first: see the note on ordering above.
  const [queue, setQueue] = useState<Recognition[]>([]);
  const [more, setMore] = useState(0);
  const [total, setTotal] = useState(0);
  const felt = useRef<string | null>(null);

  useEffect(() => {
    let gone = false;
    getRecognised()
      .then((r) => {
        if (gone) return;
        setQueue([...r.recognised].reverse());
        setTotal(r.recognised.length);
        setMore(r.more);
      })
      .catch((err) => {
        // A nicety, never a reason to show an error: the Journal and DOGS
        // still say who everyone is.
        if (!gone && err instanceof UnauthorizedError) onUnauthorized();
      });
    return () => { gone = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const current = queue[0];
  useEffect(() => {
    if (!current || felt.current === current.recognised_at) return;
    felt.current = current.recognised_at;
    haptic("filled");
  }, [current]);

  if (!current) return null;

  function read() {
    // Not awaited and not retried: if it fails, the card is simply shown
    // again next time, which is the right way round to be wrong.
    markRecognisedSeen(current.recognised_at).catch((err) => {
      if (err instanceof UnauthorizedError) onUnauthorized();
    });
    setQueue((q) => q.slice(1));
  }

  const { yours, other } = current;
  const yoursFirst = +new Date(yours.captured_at) <= +new Date(other.captured_at);
  const mine = <Side key="yours" src={yours.thumb_url} when={yours.captured_at} who="YOU" />;
  const theirs = (
    <Side key="other" src={other.thumb_url} when={other.captured_at}
      who={other.mine ? "YOU" : (other.observer ?? "SOMEONE ELSE").toUpperCase()} />
  );
  const position = total - queue.length + 1;
  const known = knownLine(current);
  const last = queue.length === 1;

  return (
    <section className="recognised" aria-live="polite" aria-label={`Recognised: ${dogName(current.individual)}`}>
      <div className="recognised-head">
        <span>RECOGNISED</span>
        {total > 1 && <span className="recognised-count">{position} / {total}</span>}
      </div>
      <p className="recognised-says">
        Your sighting from {formatDay(yours.captured_at)} is
        <strong className={current.individual.name ? "" : "anon"}>{dogTitle(current.individual)}</strong>
      </p>
      <div className="recognised-pair">
        {yoursFirst ? [mine, theirs] : [theirs, mine]}
      </div>
      <div className="recognised-apart">
        THE SAME {speciesNoun(current.individual.species).toUpperCase()} · {apart(yours.captured_at, other.captured_at)}
      </div>
      {known && <p className="recognised-known">{known}</p>}
      {last && more > 0 && (
        <p className="recognised-known">{more} more {more === 1 ? "was" : "were"} recognised before these. They are all under DOGS.</p>
      )}
      <div className="recognised-actions">
        <button className="recognised-see" onClick={() => { const id = current.individual.id; read(); onSee(id); }}>
          SEE THEM
        </button>
        <button className="link-btn" onClick={read}>{last ? "DONE" : "NEXT"}</button>
      </div>
    </section>
  );
}
