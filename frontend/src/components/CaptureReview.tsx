import { useEffect, useRef, useState } from "react";
import { UnauthorizedError } from "../api";
import { getCapture, reviewCapture, type CaptureDetail, type CaptureGroup, type CaptureInstance } from "../captureApi";

export function groupingError(instances: CaptureInstance[], assignments: Record<string, string>): string | null {
  if (instances.some((instance) => !assignments[instance.id])) return "Assign every highlighted animal to a group.";
  for (let i = 0; i < instances.length; i++) {
    for (let j = i + 1; j < instances.length; j++) {
      const a = instances[i], b = instances[j];
      if (assignments[a.id] !== assignments[b.id]) continue;
      if (a.photo_id === b.photo_id || a.co_visible_instance_ids?.includes(b.id) || b.co_visible_instance_ids?.includes(a.id)) {
        return "Animals visible together must belong to different groups.";
      }
      if (a.species !== b.species) return "Different species must belong to different groups.";
    }
  }
  return null;
}

function Evidence({ instance }: { instance: CaptureInstance }) {
  const box = instance.bbox;
  return <div className="capture-evidence">
    <img src={instance.source_url && box ? instance.source_url : instance.thumb_url} alt={`Highlighted ${instance.species} evidence`} />
    {instance.source_url && box && <span className="capture-box" style={{ left: `${box[0] * 100}%`, top: `${box[1] * 100}%`, width: `${(box[2] - box[0]) * 100}%`, height: `${(box[3] - box[1]) * 100}%` }} />}
  </div>;
}

export default function CaptureReview({ captureId, onClose, onSaved, onUnauthorized }: {
  captureId: string; onClose: () => void; onSaved: () => void; onUnauthorized: () => void;
}) {
  const [detail, setDetail] = useState<CaptureDetail | null>(null);
  const [assignments, setAssignments] = useState<Record<string, string>>({});
  const [attrs, setAttrs] = useState<Record<string, Omit<CaptureGroup, "instance_ids">>>({});
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [revision, setRevision] = useState(0);
  const unauthorizedRef = useRef(onUnauthorized);
  unauthorizedRef.current = onUnauthorized;
  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    setDetail(null);
    getCapture(captureId, controller.signal).then((result) => {
      if (controller.signal.aborted) return;
      setDetail(result);
      const tracks = new Map<string, string>();
      const savedAssignments = Object.fromEntries(result.groups.flatMap((group, index) => group.instance_ids.map((id) => [id, String(index + 1)])));
      setAttrs(Object.fromEntries(result.groups.map(({ instance_ids: _ids, ...details }, index) => [String(index + 1), details])));
      setAssignments(Object.fromEntries(result.instances.map((instance) => {
        const key = instance.track_id ?? instance.id;
        if (!tracks.has(key)) tracks.set(key, String(tracks.size + 1));
        return [instance.id, savedAssignments[instance.id] ?? tracks.get(key)!];
      })));
    }).catch((err: unknown) => {
      if (controller.signal.aborted) return;
      if (err instanceof UnauthorizedError) unauthorizedRef.current();
      setError("Couldn't load the private evidence. Try again.");
    });
    return () => controller.abort();
  }, [captureId, revision]);
  const groups = [...new Set(Object.values(assignments))].sort((a, b) => Number(a) - Number(b));
  const invalid = detail ? groupingError(detail.instances, assignments) : null;
  const canSave = !!detail && (detail.processing_state === "needs_review" || detail.processing_state === "ready") && !error && !invalid && !saving && detail.instances.length > 0;
  async function save() {
    if (!detail || !canSave) return;
    setSaving(true); setError(null);
    try {
      await reviewCapture(captureId, groups.map((group) => ({ ...attrs[group], instance_ids: detail.instances.filter((instance) => assignments[instance.id] === group).map((instance) => instance.id) })), detail.revision);
      onSaved();
    } catch (err) {
      if (err instanceof UnauthorizedError) unauthorizedRef.current();
      setError("Couldn't confirm that this review saved. Reload evidence to check its latest status before retrying.");
    } finally { setSaving(false); }
  }
  return <div className="capture-review-overlay" role="dialog" aria-modal="true" aria-label="Review animals in upload">
    <section className="capture-review">
      <button className="link-btn" onClick={onClose} disabled={saving}>Close</button>
      <h2>Separate the animals</h2>
      <p>{detail?.processing_state === "ready" ? "Published associations are locked; you can update each animal’s details." : "Private review before publication. Put views of the same animal in one group. Split an incorrect association by assigning its evidence to another group."} These groups apply only to this upload, not identities across uploads.</p>
      {error && <p role="alert">{error} <button disabled={saving} onClick={() => setRevision((n) => n + 1)}>Reload evidence</button></p>}
      {!detail && !error && <p>Loading private evidence…</p>}
      {detail && <>
        <p className="hint">Shared time: {new Date(detail.captured_at).toLocaleString()}. Location is shared unchanged across all entries.</p>
        {detail.note && <p>Shared note: {detail.note}</p>}
        <div className="capture-evidence-grid">{detail.instances.map((instance, index) => <article key={instance.id}>
          <Evidence instance={instance} />
          <label>Evidence {index + 1} · {instance.species}{instance.timestamp_ms != null ? ` · ${(instance.timestamp_ms / 1000).toFixed(1)}s` : ""}
            <select aria-label={`Group for evidence ${index + 1}`} value={assignments[instance.id] ?? ""} disabled={saving || detail.processing_state === "ready"} onChange={(e) => setAssignments((current) => ({ ...current, [instance.id]: e.target.value }))}>
              {detail.instances.map((_, i) => <option key={i} value={String(i + 1)}>Animal {i + 1}</option>)}
            </select>
          </label>
        </article>)}</div>
        {invalid && <p role="alert">{invalid}</p>}
        <h3>Details for each animal</h3>
        {groups.map((group) => <fieldset key={group} disabled={saving}>
          <legend>Animal {group}</legend>
          {([ ["sex", ["male", "female", "unsure"]], ["ear_notch", ["none", "left", "right", "unsure"]], ["condition", ["healthy", "injured", "unsure"]] ] as const).map(([field, options]) => <label key={field}>{field.replace("_", " ")}
            <select value={attrs[group]?.[field] ?? ""} onChange={(e) => setAttrs((current) => ({ ...current, [group]: { ...current[group], [field]: e.target.value || undefined } }))}>
              <option value="">Not recorded</option>{options.map((value) => <option key={value} value={value}>{value}</option>)}
            </select>
          </label>)}
        </fieldset>)}
        <button className="btn btn-primary" disabled={!canSave} onClick={() => void save()}>{saving ? "Saving…" : detail.processing_state === "ready" ? "Save animal details" : `Publish ${groups.length} animal ${groups.length === 1 ? "sighting" : "sightings"}`}</button>
      </>}
    </section>
  </div>;
}
