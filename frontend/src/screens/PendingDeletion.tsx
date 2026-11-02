import { useState } from "react";
import { cancelAccountDeletion, UnauthorizedError, type Account } from "../api";

/**
 * What a person whose account is scheduled for deletion sees instead of the
 * app (#179): when it goes, and the one way back.
 */
export default function PendingDeletion({ account, onKept, onSignOut, onUnauthorized }: {
  account: Account;
  onKept: () => void;
  onSignOut: () => void;
  onUnauthorized: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const when = account.purge_after
    ? new Date(account.purge_after).toLocaleDateString(undefined, { day: "numeric", month: "long", year: "numeric" })
    : null;

  async function keep() {
    setBusy(true);
    setError(null);
    try {
      await cancelAccountDeletion();
      onKept();
    } catch (err) {
      if (err instanceof UnauthorizedError) onUnauthorized();
      else setError("Couldn’t keep your account just now. Try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="empty-state pending-deletion">
      <h2>Your account is being deleted</h2>
      {account.can_cancel ? (
        <>
          <p>{when ? `It will be deleted on ${when}.` : "It will be deleted soon."} Your sightings are hidden from everyone until then.</p>
          <button className="btn" disabled={busy} onClick={() => void keep()}>Keep my account</button>
        </>
      ) : (
        <p>Deleting your account has started and can’t be undone.</p>
      )}
      {error && <p className="hint place-warning" role="alert">{error}</p>}
      <button className="link-btn" onClick={onSignOut}>sign out</button>
    </div>
  );
}
