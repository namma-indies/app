import { useEffect, useState } from "react";
import {
  getSocialQueue,
  getSocialShared,
  passForSocial,
  pickForSocial,
  removeSocialPick,
  settleTakedown,
  UnauthorizedError,
  type SocialPick,
  type SocialQueue,
  type SocialShared,
  type SocialTakedown,
} from "../api";
import { formatDay, formatWhen } from "../formatWhen";
import { speciesNoun } from "../species";

/** The moderators' POSTS screen: what people have shared, what goes out next
 * on @nammaindies, and what has gone out.
 *
 * Picking is the only check on what a photo shows -- a house number, a shop
 * sign, a face -- because the whole frame is posted. So a card shows the photo
 * at full size, exactly as it would go out, beside the caption it would carry.
 * Design: docs/specs/2026-10-24-x-daily-post-design.md.
 */

const AREA = "{area}";
const UNCERTAIN = "Posting didn't confirm. Check @nammaindies on X before anything else posts.";
const TAKEN_DOWN = "Taken down in the app — remove this post on X";

const postUrl = (id: string) => `https://x.com/nammaindies/status/${id}`;

function SharedCard({ item, onDone, onUnauthorized }: {
  item: SocialShared;
  onDone: () => void;
  onUnauthorized: () => void;
}) {
  const [photoId, setPhotoId] = useState(item.photos[0]?.photo_id);
  const [caption, setCaption] = useState(item.default_caption);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const photo = item.photos.find((p) => p.photo_id === photoId) ?? item.photos[0];
  const needsArea = caption.includes(AREA);

  async function act(run: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await run();
      onDone();
    } catch (err) {
      if (err instanceof UnauthorizedError) onUnauthorized();
      else setError(err instanceof Error ? err.message : "That didn't save. Try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="match-card social-card">
      {photo && <img className="social-photo" src={photo.url} alt="photo to post" />}
      {item.photos.length > 1 && (
        <div className="social-choices">
          {item.photos.map((p, i) => (
            <button
              key={p.photo_id}
              className={p.photo_id === photo?.photo_id ? "active" : ""}
              aria-label={`photo ${i + 1}`}
              aria-pressed={p.photo_id === photo?.photo_id}
              onClick={() => setPhotoId(p.photo_id)}
            >
              <img src={p.thumb_url} alt="" />
            </button>
          ))}
        </div>
      )}
      <div className="line">
        {speciesNoun(item.species)} · {formatWhen(item.captured_at)} · @{item.handle}
      </div>
      <label className="social-caption">
        Caption
        <textarea rows={3} value={caption} onChange={(e) => setCaption(e.target.value)} />
      </label>
      {needsArea && <p className="hint">Replace {AREA} with where they were seen.</p>}
      {error && <p className="hint" role="alert">{error}</p>}
      <div className="match-actions">
        <button className="btn-different" disabled={busy} onClick={() => act(() => passForSocial(item.sighting_id))}>
          Not this one
        </button>
        <button
          className="btn-same"
          disabled={busy || needsArea || !caption.trim() || !photo}
          onClick={() => act(() => pickForSocial(item.sighting_id, photo!.photo_id, caption))}
        >
          Queue for X
        </button>
      </div>
    </div>
  );
}

function PickRow({ pick, children }: { pick: SocialPick; children?: React.ReactNode }) {
  return (
    <div className="match-card">
      <div className="mod-head">
        <img className="mod-thumb" src={pick.thumb_url} alt="queued photo" />
        <div className="mod-meta">
          <div className="line">{pick.caption}</div>
          {children}
        </div>
      </div>
    </div>
  );
}

export const UNCONFIRMED_TAKEDOWN =
  "Its owner deleted this photo while a post of it was on its way, and X never confirmed it. Check the account: if the post is there, paste its link; if not, say nothing went out.";

/** The post number from a link to it, or the number itself. */
export function postIdFrom(text: string): string | null {
  const t = text.trim();
  const m = t.match(/\/status(?:es)?\/(\d{1,30})/) ?? t.match(/^(\d{1,30})$/);
  return m ? m[1] : null;
}

function TakedownRow({ takedown, onSettled, onUnauthorized }: {
  takedown: SocialTakedown; onSettled: () => void; onUnauthorized: () => void;
}) {
  const [link, setLink] = useState("");
  const [error, setError] = useState<string | null>(null);
  async function settle(xPostId: string | null) {
    try {
      await settleTakedown(takedown.id, xPostId);
      onSettled();
    } catch (err) {
      if (err instanceof UnauthorizedError) onUnauthorized();
      else setError(err instanceof Error ? err.message : "Couldn't save that. Try again.");
    }
  }
  if (!takedown.unconfirmed) {
    return (
      <div className="match-card">
        <p className="hint">
          A photo deleted by its owner. Its post is being taken down
          {takedown.attempts > 0 ? ` (tried ${takedown.attempts}×${takedown.last_error ? `: ${takedown.last_error}` : ""})` : ""}.
        </p>
        {takedown.x_post_id && <a className="link-btn" href={postUrl(takedown.x_post_id)} target="_blank" rel="noreferrer">View on X</a>}
      </div>
    );
  }
  const id = postIdFrom(link);
  return (
    <div className="match-card">
      <p className="under-review">{UNCONFIRMED_TAKEDOWN}</p>
      {takedown.caption && <p className="hint">“{takedown.caption}”</p>}
      <input aria-label="link to the post on X" value={link} onChange={(e) => setLink(e.target.value)} placeholder="https://x.com/…/status/…" />
      <button className="link-btn" disabled={!id} onClick={() => void settle(id)}>Take this post down</button>
      <button className="link-btn" onClick={() => void settle(null)}>Nothing went out</button>
      {error && <p className="hint place-warning" role="alert">{error}</p>}
    </div>
  );
}

export default function Social({ onUnauthorized }: { onUnauthorized: () => void }) {
  const [shared, setShared] = useState<SocialShared[] | null>(null);
  const [queue, setQueue] = useState<SocialQueue | null>(null);
  const [failed, setFailed] = useState(false);

  async function load() {
    try {
      const [s, q] = await Promise.all([getSocialShared(), getSocialQueue()]);
      setShared(s.items);
      setQueue(q);
      setFailed(false);
    } catch (err) {
      if (err instanceof UnauthorizedError) onUnauthorized();
      else setFailed(true);
    }
  }

  // A decided card leaves the list where it stands rather than via a refetch,
  // which would reorder the shared list under someone working down it. Only
  // the queue is reloaded, since that is where the pick now appears.
  async function decided(sightingId: string) {
    setShared((cur) => (cur ?? []).filter((x) => x.sighting_id !== sightingId));
    try {
      setQueue(await getSocialQueue());
    } catch (err) {
      if (err instanceof UnauthorizedError) onUnauthorized();
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function remove(id: string) {
    try {
      await removeSocialPick(id);
    } catch (err) {
      if (err instanceof UnauthorizedError) return onUnauthorized();
    }
    void load();
  }

  if (failed) return <div className="empty-state">COULDN'T LOAD THE POSTS — TRY AGAIN</div>;
  if (shared === null || queue === null) return <div className="empty-state">READING WHAT'S BEEN SHARED…</div>;

  return (
    <div className="review">
      {(queue.takedowns ?? []).length > 0 && (
        <>
          <div className="journal-head">TO TAKE DOWN FROM X</div>
          {(queue.takedowns ?? []).map((t) => (
            <TakedownRow key={t.id} takedown={t} onSettled={() => void load()} onUnauthorized={onUnauthorized} />
          ))}
        </>
      )}
      {queue.attention.length > 0 && (
        <>
          <div className="journal-head">NEEDS ATTENTION</div>
          {queue.attention.map((p) => (
            <PickRow key={p.id} pick={p}>
              <p className="under-review">{p.status === "posting" ? UNCERTAIN : p.last_error}</p>
            </PickRow>
          ))}
        </>
      )}

      <div className="journal-head">
        {shared.length} SHARED WITH THE TEAM
      </div>
      {shared.length === 0 && <p className="hint">Nothing new. Sightings people share show up here.</p>}
      {shared.map((item) => (
        <SharedCard key={item.sighting_id} item={item} onDone={() => void decided(item.sighting_id)} onUnauthorized={onUnauthorized} />
      ))}

      <div className="journal-head">{queue.queued.length} QUEUED · OLDEST POSTS FIRST</div>
      {queue.queued.map((p) => (
        <PickRow key={p.id} pick={p}>
          <button className="link-btn" onClick={() => void remove(p.id)}>Remove</button>
        </PickRow>
      ))}

      {queue.posted.length > 0 && (
        <>
          <div className="journal-head">POSTED</div>
          {queue.posted.map((p) => (
            <PickRow key={p.id} pick={p}>
              {p.flagged && <p className="under-review">{TAKEN_DOWN}</p>}
              {p.x_post_id && (
                <a className="link-btn" href={postUrl(p.x_post_id)} target="_blank" rel="noreferrer">
                  View on X{p.posted_at ? ` · ${formatDay(p.posted_at)}` : ""}
                </a>
              )}
            </PickRow>
          ))}
        </>
      )}
    </div>
  );
}
