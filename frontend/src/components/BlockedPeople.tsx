import { useEffect, useState } from "react";
import { getBlocks, unblock, UnauthorizedError, type Block } from "../api";

/** The people you have blocked, to unblock (#180). */
export default function BlockedPeople({ onClose, onChanged, onUnauthorized }: {
  onClose: () => void;
  /** After an unblock: their sightings can show again. */
  onChanged: () => void;
  onUnauthorized: () => void;
}) {
  const [blocks, setBlocks] = useState<Block[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    getBlocks().then((r) => setBlocks(r.blocks)).catch((err) => {
      if (err instanceof UnauthorizedError) onUnauthorized();
      else setFailed(true);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function lift(b: Block) {
    try {
      await unblock(b.id);
      setBlocks((cur) => (cur ?? []).filter((x) => x.id !== b.id));
      onChanged();
    } catch (err) {
      if (err instanceof UnauthorizedError) onUnauthorized();
      else setFailed(true);
    }
  }

  return (
    <div className="viewer-overlay" onClick={onClose}>
      <div className="report-sheet" role="dialog" aria-label="Blocked people" onClick={(e) => e.stopPropagation()}>
        <h2>Blocked people</h2>
        {blocks === null && !failed && <p className="hint">Loading…</p>}
        {blocks?.length === 0 && <p className="hint">You haven’t blocked anyone.</p>}
        {blocks?.map((b) => (
          <div key={b.id} className="blocked-row">
            <span>{b.name}</span>
            <button className="link-btn" onClick={() => void lift(b)}>Unblock</button>
          </div>
        ))}
        {failed && <p className="signin-error" role="alert">Couldn’t do that just now. Try again.</p>}
        <div className="actions-row">
          <button className="btn btn-secondary" onClick={onClose}>CLOSE</button>
        </div>
      </div>
    </div>
  );
}
