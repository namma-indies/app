import { useEffect, useState } from "react";
import { discardFailed, flush, listFailed, retryFailed, type QueuedItem } from "../offline/queue";

export default function FailedSightings({ onClose }: { onClose: () => void }) {
  const [items, setItems] = useState<QueuedItem[]>([]);

  useEffect(() => {
    listFailed().then(setItems);
  }, []);

  async function refresh() {
    setItems(await listFailed());
  }

  async function retry(id: number) {
    await retryFailed(id);
    await refresh();
    flush();
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
                <span className="failed-when">
                  {new Date(item.captured_at).toLocaleString()}
                </span>
                <div className="failed-actions">
                  <button className="btn btn-secondary" onClick={() => discard(item.id)}>
                    DISCARD
                  </button>
                  <button className="btn btn-primary" onClick={() => retry(item.id)}>
                    RETRY
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
