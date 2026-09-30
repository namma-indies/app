import { useEffect, useRef, useState } from "react";
import {
  getAnimalAudit, getAnimalAuditDetail, HttpError, ruleOnAnimal, ruleOnCaptureAnimal, UnauthorizedError,
  type AnimalAuditDetail, type AnimalAuditEvidence, type AnimalAuditFilter, type AnimalAuditItem,
} from "../api";

const FILTERS: { value: AnimalAuditFilter; label: string }[] = [
  { value: "all", label: "All" }, { value: "needs_review", label: "Needs review" },
  { value: "reviewed", label: "Reviewed" }, { value: "no_detection", label: "No detection" },
];
const keyOf = (item: AnimalAuditItem) => `${item.kind}:${item.id}`;
const score = (value: number | null) => value == null ? "not scored" : value.toFixed(2);
const verdictLabel = (value: boolean | null) => value == null ? "Not reviewed" : value ? "Animal present" : "No animal";

function detectorState(item: AnimalAuditItem) {
  switch (item.processing_state) {
    case "queued": return "Queued — not yet scored";
    case "processing": return "Processing — results incomplete";
    case "failed": return "Processing failed — not evidence of absence";
    case "no_animal":
    case "no_detection": return "No detection — model result, not a human verdict";
    default: return item.dog == null && item.cat == null ? "Unscored — results unavailable" : "Scored";
  }
}

export default function AnimalAudit({ onUnauthorized }: { onUnauthorized: () => void }) {
  const [filter, setFilter] = useState<AnimalAuditFilter>("all");
  return <section className="animal-audit" aria-label="Detector audit">
    <h2 className="journal-head">DETECTOR AUDIT</h2>
    <p>Model evidence and human verdicts are separate. Missing scores or boxes do not prove an animal is absent.</p>
    <div className="scope-toggle audit-filters" aria-label="Audit filters">
      {FILTERS.map(({ value, label }) => <button key={value} aria-pressed={filter === value}
        className={filter === value ? "active" : ""} onClick={() => setFilter(value)}>{label}</button>)}
    </div>
    {filter === "no_detection" && <p>Includes explicit no-animal results, zero dog/cat scores, and uploads with missing model results. Check each item's processing state before ruling.</p>}
    {filter === "needs_review" && <p>Unreviewed low or missing scores, processing failures, and captures awaiting review. High-confidence sightings are available under All.</p>}
    <AuditList key={filter} filter={filter} onUnauthorized={onUnauthorized} />
  </section>;
}

function AuditList({ filter, onUnauthorized }: { filter: AnimalAuditFilter; onUnauthorized: () => void }) {
  const [items, setItems] = useState<AnimalAuditItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [request, setRequest] = useState({ before: null as string | null, attempt: 0 });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [selected, setSelected] = useState<AnimalAuditItem | null>(null);
  const unauthorized = useRef(onUnauthorized);
  unauthorized.current = onUnauthorized;

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(false);
    getAnimalAudit(filter, request.before, controller.signal).then((page) => {
      if (controller.signal.aborted) return;
      setItems((previous) => {
        const merged = new Map((request.before ? previous : []).map((item) => [keyOf(item), item]));
        page.items.forEach((item) => merged.set(keyOf(item), item));
        return [...merged.values()];
      });
      setCursor(page.next_cursor);
    }).catch((err) => {
      if (controller.signal.aborted) return;
      if (err instanceof UnauthorizedError) unauthorized.current();
      else setError(true);
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [filter, request]);

  function updateVerdict(item: AnimalAuditItem, value: boolean) {
    setItems((current) => current.map((entry) => keyOf(entry) === keyOf(item) ? { ...entry, animal_override: value } : entry));
  }

  function advance() {
    const index = items.findIndex((item) => selected && keyOf(item) === keyOf(selected));
    setSelected(items[index + 1] ?? null);
  }

  return <>
    {selected ? <AuditDetail key={keyOf(selected)} item={selected} onUnauthorized={onUnauthorized}
      onClose={() => setSelected(null)} onSkip={advance} onVerdict={updateVerdict} /> : <>
      <div className="review audit-list">
        {items.map((item) => <article key={keyOf(item)} className="match-card">
          <div className="mod-head">
            {item.thumb_url ? <img className="mod-thumb" src={item.thumb_url} alt="Audit thumbnail" loading="lazy" /> : <div className="match-blank">NO PREVIEW</div>}
            <div className="mod-meta">
              <div>{item.kind === "capture" ? "Capture without a sighting" : "Sighting"} · {new Date(item.captured_at).toLocaleString()}</div>
              <div>{detectorState(item)}</div>
              <div>Model: DOG {score(item.dog)} · CAT {score(item.cat)}</div>
              <div className="audit-verdict">Human verdict: {verdictLabel(item.animal_override)}</div>
            </div>
          </div>
          <button className="btn btn-secondary" onClick={() => setSelected(item)}>Open evidence</button>
        </article>)}
      </div>
      {!loading && !error && items.length === 0 && <p className="empty-state">No items in this filter.</p>}
    </>}
    {loading && <p role="status">Loading audit…</p>}
    {error && <div role="alert">Couldn't load the audit. <button className="btn btn-secondary"
      onClick={() => setRequest((current) => ({ ...current, attempt: current.attempt + 1 }))}>Retry list</button></div>}
    {!selected && cursor && !error && <button className="btn btn-secondary" disabled={loading}
      onClick={() => { setLoading(true); setRequest({ before: cursor, attempt: 0 }); }}>Load more</button>}
    {filter === "needs_review" && items.some((item) => item.animal_override !== null) &&
      <p>Saved verdicts stay here until you change filters, so you can reverse a decision.</p>}
  </>;
}

function Evidence({ frame, index }: { frame: AnimalAuditEvidence; index: number }) {
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const validDimensions = Number.isFinite(frame.width) && Number.isFinite(frame.height) && frame.width > 0 && frame.height > 0;
  const boxes = frame.boxes.map((box, i) => ({ ...box, number: i + 1 })).filter(({ bbox, species }) =>
    (species === "dog" || species === "cat") && validDimensions && bbox.every(Number.isFinite) &&
    bbox[0] >= 0 && bbox[1] >= 0 && bbox[2] > bbox[0] && bbox[3] > bbox[1] && bbox[2] <= frame.width && bbox[3] <= frame.height);
  return <>
    <div className="audit-photo">
      {failed ? <div role="alert">Couldn't load this photo. <button className="btn btn-secondary" onClick={() => { setFailed(false); setAttempt(attempt + 1); }}>Retry photo</button></div> : <>
        <img key={attempt} src={frame.photo_url} alt={`Evidence frame ${index + 1}`} onError={() => setFailed(true)} />
        <div className="audit-boxes" aria-hidden="true">
          {boxes.map(({ bbox, species, number }) => <span key={number} className={`audit-box audit-box-${species}`}
            style={{ left: `${bbox[0] / frame.width * 100}%`, top: `${bbox[1] / frame.height * 100}%`,
              width: `${(bbox[2] - bbox[0]) / frame.width * 100}%`, height: `${(bbox[3] - bbox[1]) / frame.height * 100}%` }}>
            <b>{number} {species.toUpperCase()}</b>
          </span>)}
        </div>
      </>}
    </div>
    <p>Frame model: {frame.model ?? "unavailable"} · DOG {score(frame.dog)} · CAT {score(frame.cat)}</p>
    {boxes.length > 0 ? <ul className="audit-box-legend">{boxes.map(({ number, species, confidence }) =>
      <li key={number}>Box {number}: {species} · model confidence {score(confidence)}</li>)}</ul> :
      <p>No displayable dog/cat boxes for this frame. This is not proof of absence.</p>}
  </>;
}

function AuditDetail({ item, onUnauthorized, onClose, onSkip, onVerdict }: {
  item: AnimalAuditItem; onUnauthorized: () => void; onClose: () => void; onSkip: () => void;
  onVerdict: (item: AnimalAuditItem, value: boolean) => void;
}) {
  const [detail, setDetail] = useState<AnimalAuditDetail | null>(null);
  const [frameIndex, setFrameIndex] = useState(0);
  const [attempt, setAttempt] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [stale, setStale] = useState(false);
  const [saved, setSaved] = useState(false);
  const alive = useRef(false);
  const saving = useRef(false);
  const unauthorized = useRef(onUnauthorized);
  unauthorized.current = onUnauthorized;
  const heading = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    alive.current = true;
    heading.current?.focus();
    return () => { alive.current = false; };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setDetail(null);
    setError(null);
    setStale(false);
    setSaved(false);
    setFrameIndex(0);
    getAnimalAuditDetail(item.kind, item.id, controller.signal).then((result) => {
      if (!controller.signal.aborted) setDetail(result);
    }).catch((err) => {
      if (controller.signal.aborted) return;
      if (err instanceof UnauthorizedError) unauthorized.current();
      else setError(err instanceof HttpError && err.status === 409 ? "This item changed. Refresh the evidence before reviewing." : "Couldn't load the evidence.");
    });
    return () => controller.abort();
  }, [item.kind, item.id, attempt]);

  async function decide(verdict: "animal" | "no_animal") {
    if (!detail || saving.current || stale) return;
    saving.current = true;
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      if (item.kind === "sighting") await ruleOnAnimal(item.id, verdict);
      else await ruleOnCaptureAnimal(item.id, verdict);
      if (!alive.current) return;
      const value = verdict === "animal";
      setDetail((current) => current && { ...current, animal_override: value });
      onVerdict(item, value);
      setSaved(true);
    } catch (err) {
      if (!alive.current) return;
      if (err instanceof UnauthorizedError) unauthorized.current();
      else if (err instanceof HttpError && err.status === 409) {
        setStale(true);
        setError("This item changed. Refresh the evidence before reviewing; your verdict was not saved.");
      } else setError("Couldn't save the verdict. Try the action again.");
    } finally {
      saving.current = false;
      if (alive.current) setBusy(false);
    }
  }

  const frame = detail?.evidence[frameIndex];
  return <section className="audit-detail" aria-label="Animal evidence detail" aria-busy={busy}>
    <button className="btn btn-secondary" disabled={busy} onClick={onClose}>Back to audit</button>
    <h3 ref={heading} tabIndex={-1}>Review {item.kind} evidence</h3>
    {!detail && !error && <p role="status">Loading evidence…</p>}
    {error && <div role="alert">{error} {(!detail || stale) && <button className="btn btn-secondary"
      onClick={() => setAttempt(attempt + 1)}>Refresh evidence</button>}</div>}
    {detail && <>
      <p>{new Date(detail.captured_at).toLocaleString()} · {detectorState(detail)}</p>
      <p>Item model scores: DOG {score(detail.dog)} · CAT {score(detail.cat)}</p>
      <p className="audit-verdict">Human verdict: {verdictLabel(detail.animal_override)}</p>
      {frame ? <>
        <nav className="audit-frame-nav" aria-label="Evidence frames">
          <button className="btn btn-secondary" disabled={frameIndex === 0} onClick={() => setFrameIndex(frameIndex - 1)}>Previous frame</button>
          <span>Frame {frameIndex + 1} of {detail.evidence.length}</span>
          <button className="btn btn-secondary" disabled={frameIndex === detail.evidence.length - 1} onClick={() => setFrameIndex(frameIndex + 1)}>Next frame</button>
        </nav>
        <p>{frame.evidence_kind === "crop" ? "Animal crop" : "Source photo"}</p>
        {detail.kind === "sighting" && <p>Review only this sighting's animal. Boxes mark its detections; other animals in the source photo are context, not the subject of this verdict.</p>}
        <Evidence key={frame.id} frame={frame} index={frameIndex} />
      </> : <p>No photo evidence available. Use Unsure / skip rather than inferring absence.</p>}
      {detail.clip_url && <div className="audit-clip"><p>Original clip</p><video controls preload="metadata" src={detail.clip_url} aria-label="Original capture clip" /></div>}
      <p>Verdicts are reversible. They do not create an identity, publish a capture, or change reported-content rulings.</p>
      {detail.kind === "sighting" && <p>“No animal” hides this sighting from shared surfaces. “Animal present” reverses that animal-presence decision; separate report rulings still apply.</p>}
      {detail.kind === "capture" && <p className="audit-warning">This capture has no sighting. “Animal present” only records your review; it does not create a detection or sighting.</p>}
      <div className="match-actions">
        <button className="btn-different" disabled={busy || stale} onClick={() => decide("animal")}>ANIMAL PRESENT</button>
        <button className="btn-same" disabled={busy || stale} onClick={() => decide("no_animal")}>NO ANIMAL</button>
      </div>
      {saved && <p role="status">Verdict saved. You can reverse it here or revisit Reviewed.</p>}
      {busy && <p role="status">Saving verdict…</p>}
    </>}
    <button className="btn btn-secondary audit-skip" disabled={busy} onClick={onSkip}>Unsure / skip</button>
    <p className="audit-note">Skip moves to the next loaded item without saving a verdict.</p>
  </section>;
}
