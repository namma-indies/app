import { useCallback, useEffect, useRef, useState } from "react";
import {
  createCalibrationBatch, getCalibrationBatches, getCalibrationDetail, getCalibrationMetrics,
  getCalibrationPairs, HttpError, labelCalibrationPair, UnauthorizedError,
  type CalibrationBatch, type CalibrationMetrics, type CalibrationPhoto, type CalibrationSide, type CalibrationVerdict,
} from "../api";

function useRemote<T>(load: (signal: AbortSignal) => Promise<T>, onUnauthorized: () => void) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  const unauthorized = useRef(onUnauthorized);
  unauthorized.current = onUnauthorized;
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setData(null);
    setError(false);
    load(controller.signal).then((result) => {
      if (!controller.signal.aborted) setData(result);
    }).catch((err) => {
      if (controller.signal.aborted) return;
      if (err instanceof UnauthorizedError) unauthorized.current();
      else setError(true);
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [load, attempt]);
  return { data, error, loading, retry: () => setAttempt((value) => value + 1) };
}

function Retry({ subject, retry, disabled = false }: { subject: string; retry: () => void; disabled?: boolean }) {
  return <p role="alert">Couldn't load {subject}. <button className="btn btn-secondary" disabled={disabled} onClick={retry}>Retry {subject}</button></p>;
}

export default function MatchCalibration({ onUnauthorized, saving, onSavingChange }: {
  onUnauthorized: () => void; saving: boolean; onSavingChange: (saving: boolean) => void;
}) {
  const [batchOffset, setBatchOffset] = useState(0);
  const batches = useRemote(useCallback((signal: AbortSignal) => getCalibrationBatches(signal, batchOffset), [batchOffset]), onUnauthorized);
  const [created, setCreated] = useState<CalibrationBatch[]>([]);
  const [selected, setSelected] = useState("");
  const [sampleSize, setSampleSize] = useState("100");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const creating = useRef<AbortController | null>(null);
  useEffect(() => () => creating.current?.abort(), []);
  const allBatches = [...new Map([...created, ...(batches.data?.batches ?? [])].map((batch) => [batch.id, batch])).values()];
  const batch = allBatches.find((entry) => entry.id === selected) ?? allBatches[0];
  const batchId = batch?.id ?? "";
  useEffect(() => {
    if (batches.data) setSelected(batchId);
  }, [batches.data, batchId]);
  const locked = busy || saving;
  const validSize = Number.isInteger(Number(sampleSize)) && Number(sampleSize) >= 1 && Number(sampleSize) <= 500;

  async function create() {
    if (creating.current || saving || !validSize) return;
    const controller = new AbortController();
    creating.current = controller;
    setBusy(true);
    setError(null);
    try {
      const result = await createCalibrationBatch(Number(sampleSize), controller.signal);
      if (controller.signal.aborted) return;
      setCreated((previous) => [result, ...previous]);
      setSelected(result.id);
    } catch (err) {
      if (controller.signal.aborted) return;
      if (err instanceof UnauthorizedError) onUnauthorized();
      else if (err instanceof HttpError && err.status === 422) setError(`${err.detail ?? "The corpus cannot be sampled with the current limits."} This requires a corpus or calibration-limit fix; retrying alone will not help.`);
      else setError(err instanceof HttpError && err.detail ? err.detail : "Couldn't create the batch. Retry with the create button. If the response was lost, refresh past batches before creating another.");
    } finally {
      if (!controller.signal.aborted) { setBusy(false); creating.current = null; }
    }
  }

  return <section className="match-calibration" aria-label="Match calibration">
    <h2 className="journal-head">MATCH CALIBRATION</h2>
    <p>Review pairs across all users. These reversible labels never merge identities, change thresholds, or retrain the model.</p>
    <details className="calibration-method" open>
      <summary>What these numbers mean</summary>
      <p>A frozen, uniform sample of eligible pairs includes scores below the current cutoff — not a top-5 selection. It uses the current corpus of valid sightings across all users, with matching's geographic and species exclusions.</p>
      <p>Exact pairwise scores do not measure retrieval or candidate-limit failures. This is not historical replay or street-wide accuracy. An incomplete sample may skew results; multiple pairs can share animals. Unsure, conflicting, and stale labels are excluded from cutoff metrics.</p>
    </details>
    <form className="calibration-create" onSubmit={(event) => { event.preventDefault(); void create(); }}>
      <label>Sample size (1–500)<input type="number" min="1" max="500" step="1" required value={sampleSize}
        disabled={locked} onChange={(event) => setSampleSize(event.target.value)} /></label>
      <button className="btn btn-primary" disabled={locked || !validSize}>{busy ? "Creating batch…" : "Create frozen review batch"}</button>
    </form>
    {error && <p role="alert">{error}</p>}
    {batches.loading && <p role="status">Loading batches…</p>}
    {batches.error && <Retry subject="batches" retry={batches.retry} disabled={locked} />}
    <button className="btn btn-secondary" disabled={batches.loading || locked} onClick={batches.retry}>Refresh past batches</button>
    <nav className="calibration-pagination" aria-label="Batch pages">
      <button className="btn btn-secondary" disabled={batches.loading || locked || batchOffset === 0} onClick={() => { setSelected(""); setCreated([]); setBatchOffset(Math.max(0, batchOffset - 20)); }}>Newer batches</button>
      <button className="btn btn-secondary" disabled={batches.loading || locked || batches.data?.next_offset == null} onClick={() => { setSelected(""); setCreated([]); setBatchOffset(batches.data!.next_offset!); }}>Older batches</button>
    </nav>
    {allBatches.length > 0 ? <>
      <label className="calibration-batches">Review batch<select value={batchId} disabled={locked || batches.loading} onChange={(event) => setSelected(event.target.value)}>
        {allBatches.map((entry) => <option key={entry.id} value={entry.id}>{new Date(entry.created_at).toLocaleString()} · {entry.sampled_pairs} pairs · {entry.id.slice(0, 8)}</option>)}
      </select></label>
      {batch && <p>Model: {batch.model} · Policy: {batch.policy} · Radius: {batch.radius_m} m</p>}
      <BatchReview key={batchId} batchId={batchId} onUnauthorized={onUnauthorized} saving={locked || batches.loading} onSavingChange={onSavingChange} />
    </> : !batches.loading && !batches.error && <p>No batches yet. Create one to start reviewing.</p>}
  </section>;
}

function BatchReview({ batchId, onUnauthorized, saving, onSavingChange }: {
  batchId: string; onUnauthorized: () => void; saving: boolean; onSavingChange: (saving: boolean) => void;
}) {
  const [offset, setOffset] = useState(0);
  const [revision, setRevision] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const pairs = useRemote(useCallback((signal: AbortSignal) => getCalibrationPairs(batchId, offset, signal), [batchId, offset, revision]), onUnauthorized);
  const metrics = useRemote(useCallback((signal: AbortSignal) => getCalibrationMetrics(batchId, signal), [batchId, revision]), onUnauthorized);
  const refresh = () => setRevision((value) => value + 1);
  const pageItems = useRef({ offset, items: pairs.data?.items ?? [] });
  if (pairs.data) pageItems.current = { offset, items: pairs.data.items };
  function skip() {
    const items = pageItems.current.offset === offset ? pageItems.current.items : [];
    const index = items.findIndex((entry) => entry.id === selected);
    setSelected(items[index + 1]?.id ?? null);
  }
  return <>
    {selected ? <PairDetail key={selected} batchId={batchId} pairId={selected} onUnauthorized={onUnauthorized}
      onSaved={refresh} onSkip={skip} onClose={() => setSelected(null)} busy={saving} onSavingChange={onSavingChange} /> : <section aria-label="Sampled pairs">
      <h3>Sampled pairs</h3>
      {pairs.loading && <p role="status">Loading pairs…</p>}
      {pairs.error && <Retry subject="pairs" retry={pairs.retry} disabled={saving} />}
      {pairs.data && <>
        {pairs.data.items.length === 0 && <p>No pairs on this page.</p>}
        <ol className="calibration-pairs" start={offset + 1}>
          {pairs.data.items.map((pair, index) => <li key={pair.id} className="match-card">
            <p>Model score: {pair.score.toFixed(3)} · Status: {pair.status}</p>
            <p>My verdict: {pair.my_verdict ?? "Not reviewed"}</p>
            <button className="btn btn-secondary" onClick={() => setSelected(pair.id)}>Review pair {offset + index + 1}</button>
          </li>)}
        </ol>
      </>}
      <nav className="calibration-pagination" aria-label="Pair pages">
        <button className="btn btn-secondary" disabled={pairs.loading || offset === 0} onClick={() => setOffset(Math.max(0, offset - 20))}>Previous pairs</button>
        <span>Page {Math.floor(offset / 20) + 1}</span>
        <button className="btn btn-secondary" disabled={pairs.loading || pairs.data?.next_offset == null} onClick={() => setOffset(pairs.data!.next_offset!)}>Next pairs</button>
      </nav>
    </section>}
    {metrics.loading && <p role="status">Loading calibration metrics…</p>}
    {metrics.error && <Retry subject="metrics" retry={metrics.retry} disabled={saving} />}
    {metrics.data && <Metrics metrics={metrics.data} />}
  </>;
}

function Metrics({ metrics: m }: { metrics: CalibrationMetrics }) {
  const percent = (value: number | null) => value == null ? "Not enough labels" : `${(value * 100).toFixed(1)}%`;
  const counts = [
    ["Eligible", m.eligible_pairs], ["Sampled", m.sampled_pairs], ["Reviewed", m.reviewed_pairs],
    ["Same", m.same], ["Different", m.different], ["Unsure", m.unsure], ["Conflict", m.conflict], ["Stale", m.stale], ["Unreviewed", m.unreviewed],
  ];
  return <section aria-label="Calibration metrics">
    <h3>Calibration metrics</h3>
    <dl className="calibration-counts">{counts.map(([label, count]) => <div key={label}><dt>{label}</dt><dd>{count}</dd></div>)}</dl>
    <p>Current cutoff: {m.current_threshold}. Predictions count as a match at or above each cutoff.</p>
    <div className="calibration-table" role="region" aria-label="Cutoff metrics" tabIndex={0}>
      <table><caption>Cutoffs on reviewed, usable pairs only</caption><thead><tr>
        <th scope="col">Cutoff</th><th scope="col">TP</th><th scope="col">FP</th><th scope="col">FN</th><th scope="col">TN</th>
        <th scope="col">Precision</th><th scope="col">Recall on reviewed pairs</th>
      </tr></thead><tbody>{m.cutoffs.map((row) => <tr key={row.threshold} className={row.threshold === m.current_threshold ? "calibration-current" : ""}>
        <th scope="row">{row.threshold}{row.threshold === m.current_threshold && " (current)"}</th>
        <td>{row.tp}</td><td>{row.fp}</td><td>{row.fn}</td><td>{row.tn}</td><td>{percent(row.precision)}</td><td>{percent(row.recall)}</td>
      </tr>)}</tbody></table>
    </div>
    <p>TP / FP: same / different pairs predicted to match. FN / TN: same / different pairs below the cutoff.</p>
    <p>Exploratory suggested cutoff: {m.suggested_threshold == null || m.same < 20 || m.different < 20 ? "Not enough labels" : m.suggested_threshold}.</p>
    <p>{m.suggestion_note}</p>
    <p>A suggestion requires at least 20 same and 20 different labels. It is exploratory, not an automatic threshold change or retraining.</p>
  </section>;
}

function Photo({ photo, label, onReady, onRetry, busy }: {
  photo: CalibrationPhoto; label: string; onReady: (ready: boolean) => void; onRetry: () => void; busy: boolean;
}) {
  const [failed, setFailed] = useState(false);
  const [naturalSize, setNaturalSize] = useState({ width: 0, height: 0 });
  const width = photo.width || naturalSize.width;
  const height = photo.height || naturalSize.height;
  const box = photo.bbox;
  const validBox = Array.isArray(box) && box.length === 4 && width > 0 && height > 0 && box.every(Number.isFinite)
    && box[0] >= 0 && box[1] >= 0 && box[2] > box[0] && box[3] > box[1] && box[2] <= width && box[3] <= height;
  return <>
    <div className="calibration-photo">
      {failed ? <p role="alert">Couldn't load this photo. <button className="btn btn-secondary" disabled={busy} onClick={onRetry}>Retry photo</button></p> : <>
        <img src={photo.photo_url} alt={label} onError={() => { setFailed(true); onReady(false); }} onLoad={(event) => {
          const { naturalWidth: width, naturalHeight: height } = event.currentTarget;
          setNaturalSize({ width, height });
          onReady(width > 0 && height > 0);
        }} />
        {validBox && <span className="calibration-box" aria-label="Animal focus box" role="img" style={{
          left: `${box[0] / width * 100}%`, top: `${box[1] / height * 100}%`,
          width: `${(box[2] - box[0]) / width * 100}%`, height: `${(box[3] - box[1]) / height * 100}%`,
        }} />}
      </>}
    </div>
    <p>{validBox ? "Box marks the animal to compare." : "No focus box available. Use Unsure if the animal is ambiguous."}</p>
  </>;
}

function Side({ side, label, onReady, onRetry, busy }: {
  side: CalibrationSide; label: string; onReady: (ready: boolean) => void; onRetry: () => void; busy: boolean;
}) {
  const [index, setIndex] = useState(0);
  function changeFrame(next: number) {
    onReady(false);
    setIndex(next);
  }
  const photo = side.photos[index];
  return <section aria-label={label}>
    <h4>{label}</h4><p>Species: {side.species ?? "Unknown"}</p>
    {photo ? <>
      <Photo key={`${photo.id}:${photo.photo_url}`} photo={photo} label={`${label} frame ${index + 1}`} onReady={onReady} onRetry={onRetry} busy={busy} />
      <nav className="calibration-frame-nav" aria-label={`${label} frames`}>
        <button className="btn btn-secondary" aria-label={`${label} previous frame`} disabled={busy || index === 0} onClick={() => changeFrame(index - 1)}>Previous</button>
        <span>Frame {index + 1} of {side.photos.length}</span>
        <button className="btn btn-secondary" aria-label={`${label} next frame`} disabled={busy || index === side.photos.length - 1} onClick={() => changeFrame(index + 1)}>Next</button>
      </nav>
    </> : <p>No photos available. Use Unsure rather than guessing.</p>}
  </section>;
}

function PairDetail({ batchId, pairId, onUnauthorized, onSaved, onSkip, onClose, busy, onSavingChange }: {
  batchId: string; pairId: string; onUnauthorized: () => void; onSaved: () => void; onSkip: () => void; onClose: () => void;
  busy: boolean; onSavingChange: (saving: boolean) => void;
}) {
  const detail = useRemote(useCallback((signal: AbortSignal) => getCalibrationDetail(batchId, pairId, signal), [batchId, pairId]), onUnauthorized);
  const [leftReady, setLeftReady] = useState(false);
  const [rightReady, setRightReady] = useState(false);
  function refreshEvidence() {
    setLeftReady(false);
    setRightReady(false);
    detail.retry();
  }
  const [stale, setStale] = useState(false);
  const [error, setError] = useState(false);
  const [saved, setSaved] = useState(false);
  const saving = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  const isStale = stale || detail.data?.status === "stale";
  const imagesReady = leftReady && rightReady;
  async function decide(verdict: CalibrationVerdict) {
    if (!detail.data || detail.loading || busy || saving.current || isStale || (verdict !== "unsure" && !imagesReady)) return;
    const controller = new AbortController();
    saving.current = controller;
    onSavingChange(true); setError(false); setSaved(false);
    try {
      await labelCalibrationPair(batchId, pairId, verdict, detail.data.evidence_token, controller.signal);
      if (!mounted.current) return;
      setSaved(true);
      refreshEvidence();
      onSaved();
    } catch (err) {
      if (!mounted.current) return;
      if (err instanceof UnauthorizedError) onUnauthorized();
      else if (err instanceof HttpError && err.status === 409) { setStale(true); onSaved(); }
      else setError(true);
    } finally {
      saving.current = null;
      onSavingChange(false);
    }
  }
  return <section className="calibration-detail" aria-label="Pair evidence" aria-busy={busy}>
    <button className="btn btn-secondary" disabled={busy} onClick={onClose}>Back to pairs</button>
    <h3>Compare these animals</h3>
    {detail.loading && <p role="status">Loading pair evidence…</p>}
    {detail.error && <Retry subject="evidence" retry={refreshEvidence} disabled={busy} />}
    {isStale && <p role="alert">This frozen evidence is stale. Labels are disabled; create a fresh batch to review current evidence.</p>}
    {detail.data && <>
      <p>Model score: {detail.data.score.toFixed(3)} · Pair status: {isStale ? "stale" : detail.data.status}</p>
      <p>My saved verdict: {detail.data.my_verdict ?? "Not reviewed"}</p>
      {detail.data.status === "conflict" && !isStale && <p role="alert">Moderators disagree on this pair. This conflict is excluded from metrics; you can revise your own verdict.</p>}
      {!isStale && <div className="calibration-evidence">
        <Side side={detail.data.left} label="Query animal" onReady={setLeftReady} onRetry={refreshEvidence} busy={busy} />
        <Side side={detail.data.right} label="Comparison animal" onReady={setRightReady} onRetry={refreshEvidence} busy={busy} />
      </div>}
      <p>Compare the indicated animals, not other animals in the frame. All contributor photos are available above.</p>
      {!isStale && !imagesReady && <p>Load the current photo on both sides to choose Same or Different. You can still choose Unsure or Skip.</p>}
      <p>Saving changes only your calibration label. You can reverse it here or revisit this batch.</p>
      <div className="match-actions calibration-actions">
        <button className="btn-different" disabled={busy || isStale || detail.loading || !imagesReady} onClick={() => decide("same")}>SAME ANIMAL</button>
        <button className="btn-same" disabled={busy || isStale || detail.loading || !imagesReady} onClick={() => decide("different")}>DIFFERENT ANIMALS</button>
        <button className="btn btn-secondary" disabled={busy || isStale || detail.loading} onClick={() => decide("unsure")}>UNSURE</button>
      </div>
    </>}
    {error && <p role="alert">Couldn't save the label. The server may have saved it if the response was lost. Reload the saved verdict or retry the same verdict before making a correction. <button className="btn btn-secondary" disabled={busy} onClick={() => { refreshEvidence(); onSaved(); }}>Reload saved verdict</button></p>}
    {saved && <p role="status">Label saved. Refreshing metrics; your verdict is reversible.</p>}
    {busy && <p role="status">Saving label…</p>}
    <button className="btn btn-secondary" disabled={busy} onClick={onSkip}>Skip — no label</button>
    <p>Skip moves to the next pair on this page without writing anything. Unsure saves an explicit label.</p>
  </section>;
}
