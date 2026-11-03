import { useEffect, useState } from "react";
import { getModerationAlertStatus, getModerationAudit, reviewSighting, UnauthorizedError,
  type ModerationAction, type ModerationAlertStatus } from "../api";
import "./ModerationOperations.css";
import { useReloadGuard } from "../update/reloadGuard";

export function ModerationAlerts({ onUnauthorized }: { onUnauthorized: () => void }) {
  const [status, setStatus] = useState<ModerationAlertStatus | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let active = true;
    const refresh = () => getModerationAlertStatus().then((s) => {
      if (active) { setStatus(s); setFailed(false); }
    }).catch((e) => {
      if (!active) return;
      if (e instanceof UnauthorizedError) onUnauthorized();
      else setFailed(true);
    });
    void refresh();
    const timer = setInterval(refresh, 60_000);
    return () => { active = false; clearInterval(timer); };
  }, [onUnauthorized]);
  if (failed) return <p className="mod-ops-notice">Couldn't check moderator email delivery.</p>;
  if (!status) return null;
  return <div className="mod-ops-notice" role="status">
    {!status.delivery_configured && <strong>Moderator email delivery is not configured. </strong>}
    {status.failed_mail > 0 && <strong>{status.failed_mail} EMAIL(S) RETRYING. </strong>}
    {status.pending_events} alert(s) waiting · {status.pending_mail} email(s) queued
  </div>;
}

export function ModerationAudit({ onUnauthorized }: { onUnauthorized: () => void }) {
  const [items, setItems] = useState<ModerationAction[] | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  useReloadGuard(busy);
  async function load(before?: string) {
    setBusy(true);
    setFailed(false);
    try {
      const result = await getModerationAudit(before);
      setItems((cur) => before ? [...(cur ?? []), ...result.items] : result.items);
      setCursor(result.next_cursor);
    } catch (e) {
      if (e instanceof UnauthorizedError) onUnauthorized();
      else setFailed(true);
    } finally { setBusy(false); }
  }
  useEffect(() => { void load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function restore(item: ModerationAction) {
    const reason = prompt("Restore this sighting to shared views? Reason (required for the audit log):");
    if (!reason?.trim()) return;
    setBusy(true);
    try {
      await reviewSighting(item.target_id, "valid", reason.trim(), "unhide");
      await load();
    } catch (e) {
      if (e instanceof UnauthorizedError) onUnauthorized();
      else setFailed(true);
    } finally { setBusy(false); }
  }

  return <div className="review">
    <p className="mod-ops-notice">Decisions are recorded with who acted, when and why. Restoring a sighting makes it visible again.</p>
    {failed && <p role="alert">Couldn't load or update the audit log. <button onClick={() => void load()}>TRY AGAIN</button></p>}
    {items === null && !failed && <p>READING DECISIONS…</p>}
    {items?.length === 0 && <p>NO MODERATOR ACTIONS YET</p>}
    {items?.map((item) => <article className="match-card mod-audit-card" key={item.id}>
      <strong>{item.action.toUpperCase()} · {item.target_kind}</strong>
      <p>{item.moderator ?? item.moderator_id} · <time dateTime={item.created_at}>{new Date(item.created_at).toLocaleString()}</time></p>
      <p>{item.reason}</p>
      <p className="mod-target-id">{item.target_id}</p>
      <p>{item.previous_state ?? "—"} → {item.new_state ?? "—"}</p>
      {item.target_kind === "sighting" && item.new_state === "rejected" && <button disabled={busy}
        onClick={() => void restore(item)}>RESTORE SIGHTING</button>}
    </article>)}
    {cursor && <button disabled={busy} onClick={() => void load(cursor)}>OLDER DECISIONS</button>}
  </div>;
}
