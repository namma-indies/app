import { useEffect, useState } from "react";
import { discardFailed, flush, listFailed, retryFailed, type QueuedItem } from "../offline/queue";

export default function FailedSightings({ onClose }: { onClose: () => void }) {
  const [items, setItems] = useState<QueuedItem[]>([]);
  const [thumbUrls, setThumbUrls] = useState<Record<number, string>>({});
  const [retrying, setRetrying] = useState<number | null>(null);

  useEffect(() => {
    listFailed().then(setItems);
  }, []);

  // Build one object URL per item so the user can tell which dog is which
  // before discarding -- destroying the only copy of that photo. Revoke on
  // every list change/unmount so we don't leak memory.
  useEffect(() => {
    const urls: Record<number, string> = {};
    for (const item of items) {
      if (item.photos?.[0]) urls[item.id] = URL.createObjectURL(item.photos[0]);
    }
    setThumbUrls(urls);
    return () => {
      Object.values(urls).forEach((url) => URL.revokeObjectURL(url));
    };
  }, [items]);

  async function refresh() {
    setItems(await listFailed());
  }

  async function retry(id: number) {
    // Refresh only after the flush settles. Refreshing first drops the item
    // out of the failed list for as long as it is pending, so the sheet
    // rendered "All caught up" over a retry that was still in flight -- and
    // stayed that way when it failed again, since nothing refreshes the sheet
    // once the flush finishes. A capture must never look synced when it isn't.
    setRetrying(id);
    try {
      await retryFailed(id);
      await flush().catch(() => {});
    } finally {
      await refresh();
      setRetrying(null);
    }
  }

  async function discard(id: number) {
    await discardFailed(id);
    await refresh();
  }

  return (
    <div className="viewer-overlay" onClick={onClose}>
      <div className="failed-sheet" onClick={(e) => e.stopPropagation()}>
        <h2>Couldn't sync</h2>
        {items.length === 0 ? (
          <p className="hint">All caught up.</p>
        ) : (
          <ul className="failed-list">
            {items.map((item) => (
              <li key={item.id}>
                <div className="failed-info">
                  {thumbUrls[item.id] && (
                    <img className="failed-thumb" src={thumbUrls[item.id]} alt="" />
                  )}
                  <span className="failed-when">
                    {new Date(item.captured_at).toLocaleString()}
                  </span>
                </div>
                <div className="failed-actions">
                  <button
                    className="btn btn-secondary"
                    onClick={() => discard(item.id)}
                    disabled={retrying !== null}
                  >
                    DISCARD
                  </button>
                  <button
                    className="btn btn-primary"
                    onClick={() => retry(item.id)}
                    disabled={retrying !== null}
                  >
                    {retrying === item.id ? <span className="spinner" /> : "RETRY"}
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
        <button className="btn btn-secondary" onClick={onClose}>
          CLOSE
        </button>
      </div>
    </div>
  );
}
