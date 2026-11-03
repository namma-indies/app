import { useEffect, useState } from "react";
import { decideGrievance, getGrievances, UnauthorizedError, type Grievance, type GrievanceKind } from "../api";
import { useReloadGuard } from "../update/reloadGuard";

const LABELS: Record<GrievanceKind, string> = { takedown: "TAKEDOWN", privacy: "PRIVACY",
  intimate_imagery: "NON-CONSENSUAL INTIMATE IMAGERY", government_order: "GOVERNMENT / COURT ORDER", other: "OTHER" };

export default function Grievances({ onUnauthorized }: { onUnauthorized: () => void }) {
  const [items, setItems] = useState<Grievance[] | null>(null);
  const [legalNote, setLegalNote] = useState("");
  const [failed, setFailed] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [targets, setTargets] = useState<Record<string, string>>({});
  const [includeClosed, setIncludeClosed] = useState(false);
  const [reference, setReference] = useState("");
  const [search, setSearch] = useState("");
  const [now, setNow] = useState(Date.now);
  useReloadGuard(busy !== null || Object.values(reasons).some((value) => !!value.trim())
    || Object.keys(targets).length > 0 || reference.trim().toUpperCase() !== search);

  async function load() {
    try {
      const result = await getGrievances({ includeClosed, reference: search || undefined });
      setItems(result.items); setLegalNote(result.legal_note); setFailed(false);
    } catch (e) {
      if (e instanceof UnauthorizedError) onUnauthorized();
      else setFailed(true);
    }
  }
  useEffect(() => { void load(); }, [includeClosed, search]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(timer);
  }, []);

  function deadline(label: string, due: string, completed: string | null, closed = false, overdue = false) {
    return <p className="mod-clock"><strong>{label}: </strong>
      {completed ? <>DONE · <time dateTime={completed}>{new Date(completed).toLocaleString()}</time>
        {Date.parse(completed) > Date.parse(due) && <strong> · LATE</strong>}</>
        : closed ? <strong>NO REMOVAL RECORDED</strong> : <>
          {(overdue || now >= Date.parse(due)) && <strong>OVERDUE · </strong>}
          <time dateTime={due}>{new Date(due).toLocaleString()}</time>
        </>}
    </p>;
  }

  async function act(item: Grievance, action: "hide" | "resolve" | "dismiss") {
    const reason = reasons[item.id]?.trim();
    if (!reason) return;
    setBusy(item.id); setActionError(null);
    try {
      await decideGrievance(item.id, action, reason, action === "hide" ? targets[item.id]?.trim() || item.sighting_id || undefined : undefined);
      await load();
      setReasons((cur) => { const next = { ...cur }; delete next[item.id]; return next; });
      setTargets((cur) => { const next = { ...cur }; delete next[item.id]; return next; });
    } catch (e) {
      if (e instanceof UnauthorizedError) onUnauthorized();
      else setActionError(e instanceof Error ? e.message : "Couldn't record that decision.");
    } finally { setBusy(null); }
  }

  return <div className="review">
    <p className="mod-ops-notice">{legalNote || "Provisional tracking targets: confirm every deadline and its applicability with a lawyer."}</p>
    <form className="mod-grievance-search" onSubmit={(e) => { e.preventDefault(); setSearch(reference.trim().toUpperCase()); }}>
      <label>Reference number <input aria-label="Reference number" value={reference} maxLength={40} onChange={(e) => setReference(e.target.value)} /></label>
      <button type="submit">FIND</button>
    </form>
    <label className="mod-ops-notice"><input type="checkbox" checked={includeClosed} onChange={(e) => setIncludeClosed(e.target.checked)} /> Include closed complaints</label>
    <button className="link-btn" onClick={() => void load()}>REFRESH COMPLAINTS</button>
    {failed && <p role="alert">Couldn't load complaints. <button onClick={() => void load()}>TRY AGAIN</button></p>}
    {actionError && <p role="alert">{actionError}</p>}
    {items === null && !failed && <p>READING COMPLAINTS…</p>}
    {items?.length === 0 && <p>NO COMPLAINTS WAITING</p>}
    {items?.map((item) => <article key={item.id} className="match-card mod-audit-card">
      <h3>{LABELS[item.kind]}</h3>
      <strong className="mod-target-id">{item.reference} · {item.status.toUpperCase()}</strong>
      <p>Received <time dateTime={item.received_at}>{new Date(item.received_at).toLocaleString()}</time>
        {` · ${Math.max(0, Math.floor((now - Date.parse(item.received_at)) / 60_000))} min ago`}</p>
      <p className="mod-target-id">{item.target}</p>
      {!!item.photos?.length && <div className="mod-grievance-photos">{item.photos.map((photo) =>
        <a key={photo.photo_url} href={photo.photo_url} target="_blank" rel="noreferrer">
          <img src={photo.thumb_url} alt="Linked sighting frame; open full photo" loading="lazy" />
        </a>)}</div>}
      <p className="mod-grievance-details">{item.details}</p>
      <p>Contact: <a href={`mailto:${encodeURIComponent(item.contact_email)}`}>{item.contact_email}</a></p>
      {deadline("ACKNOWLEDGE", item.acknowledge_due_at, item.acknowledged_at, false, item.acknowledgement_overdue)}
      {!item.acknowledged_at && <p>{item.acknowledgement_error ? "Acknowledgement email failed; retrying." : "Acknowledgement email queued."}</p>}
      {deadline("RESOLVE", item.resolve_due_at, item.resolved_at, false, item.resolution_overdue)}
      {item.removal_due_at && deadline(item.kind === "government_order" ? "ORDER ACTION" : "REMOVAL REVIEW",
        item.removal_due_at, item.removed_at, item.resolved_at !== null, item.removal_overdue)}
      {item.resolution_reason && <p>Decision: {item.resolution_reason}</p>}
      {!item.resolved_at && <>
        <p className="mod-ops-notice">Check the content and any order's authenticity. Contact the complainant with your response. Hiding content keeps the complaint open for resolution.</p>
        <label className="mod-decision-reason">Sighting ID to hide
          <input aria-label="Sighting ID to hide" value={targets[item.id] ?? item.sighting_id ?? ""}
            onChange={(e) => setTargets((cur) => ({ ...cur, [item.id]: e.target.value }))} />
        </label>
        <label className="mod-decision-reason">Decision reason (required)
          <textarea aria-label="Decision reason" rows={2} maxLength={500} value={reasons[item.id] ?? ""}
            onChange={(e) => setReasons((cur) => ({ ...cur, [item.id]: e.target.value }))} />
        </label>
        <div className="mod-grievance-actions">
          <button disabled={busy !== null || !reasons[item.id]?.trim() || !(targets[item.id] ?? item.sighting_id)?.trim()}
            onClick={() => void act(item, "hide")}>HIDE LINKED CONTENT</button>
          <button disabled={busy !== null || !reasons[item.id]?.trim()} onClick={() => void act(item, "resolve")}>MARK RESOLVED</button>
          <button disabled={busy !== null || !reasons[item.id]?.trim()} onClick={() => void act(item, "dismiss")}>DISMISS</button>
        </div>
      </>}
    </article>)}
  </div>;
}
