import { useEffect, useState } from "react";
import { getAccount, requestAccountDeletion, UnauthorizedError, type Account } from "../api";
import LegalLinks from "./LegalLinks";

/**
 * Delete my account (#179). Says what goes and what stays, asks for "delete"
 * typed out, then schedules it: hidden at once, erased after the grace
 * period, which the pending screen counts down and can undo.
 */
export default function DeleteAccount({ onClose, onScheduled, onUnauthorized }: {
  onClose: () => void;
  onScheduled: (account: Account) => void;
  onUnauthorized: () => void;
}) {
  const [account, setAccount] = useState<Account | null>(null);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    getAccount().then(setAccount).catch((err) => {
      if (err instanceof UnauthorizedError) onUnauthorized();
    });
    // Once, on opening: the counts do not change while the sheet is up.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function confirmDelete() {
    setBusy(true);
    setFailed(false);
    try {
      onScheduled(await requestAccountDeletion());
    } catch (err) {
      if (err instanceof UnauthorizedError) onUnauthorized();
      else setFailed(true);
    } finally {
      setBusy(false);
    }
  }

  const n = account?.counts;
  return (
    <div className="viewer-overlay" onClick={onClose}>
      <div className="viewer-card delete-account" role="dialog" aria-label="Delete account" onClick={(e) => e.stopPropagation()}>
        <h2>Delete your account?</h2>
        {n && <p>You have logged {n.sightings} {n.sightings === 1 ? "sighting" : "sightings"}{n.voice_notes ? ` and ${n.voice_notes} added voice notes` : ""}.</p>}
        <ul>
          <li><strong>Deleted:</strong> your photos and videos, your sightings, your voice notes and their words, your reports, your name and email address.</li>
          <li><strong>Kept, without your name:</strong> dogs other people logged too stay with their photos. Your answers on other people’s matches stay, unnamed.</li>
          <li><strong>When:</strong> your sightings are hidden from everyone now. Everything is deleted in {account?.grace_days ?? 7} days; until then you can sign in and keep your account.</li>
        </ul>
        <label htmlFor="confirm-delete">Type <strong>delete</strong> to confirm</label>
        <input id="confirm-delete" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" autoCapitalize="none" />
        {failed && <p className="hint place-warning" role="alert">Couldn’t do that just now. Try again.</p>}
        <div className="viewer-delete">
          <button type="button" className="link-btn" onClick={onClose}>Cancel</button>
          <button type="button" className="link-btn" disabled={busy || typed.trim().toLowerCase() !== "delete"} onClick={() => void confirmDelete()}>
            Delete my account
          </button>
        </div>
        <LegalLinks />
      </div>
    </div>
  );
}
