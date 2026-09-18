import { useEffect, useRef, useState, type CSSProperties } from "react";
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

const evidenceColors = ["#ffda47", "#66d9ff", "#ff8cc6", "#a6ed83", "#c8a2ff", "#ffad66"];
const evidenceStyle = (index: number): CSSProperties => ({ "--evidence-color": evidenceColors[index % evidenceColors.length] } as CSSProperties);
const boxStyle = (box: NonNullable<CaptureInstance["bbox"]>): CSSProperties => ({
  left: `${box[0] * 100}%`, top: `${box[1] * 100}%`, width: `${(box[2] - box[0]) * 100}%`, height: `${(box[3] - box[1]) * 100}%`,
});

function Evidence({ instance }: { instance: CaptureInstance }) {
  const box = instance.source_bbox ? undefined : instance.bbox;
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
  const [selectedId, setSelectedId] = useState<string | null>(null);
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
      setSelectedId(result.instances[0]?.id ?? null);
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
  const sources = new Map<string, { instance: CaptureInstance; index: number }[]>();
  detail?.instances.forEach((instance, index) => {
    const items = sources.get(instance.photo_id) ?? [];
    items.push({ instance, index });
    sources.set(instance.photo_id, items);
  });
  const groups = [...new Set(Object.values(assignments))].sort((a, b) => Number(a) - Number(b));
  const speciesCounts = new Map<string, number>();
  const groupSpecies = new Map(groups.map((group) => [group, new Set(detail?.instances.filter((instance) => assignments[instance.id] === group).map((instance) => instance.species))]));
  const groupLabels = new Map(groups.map((group) => {
    const species = groupSpecies.get(group)!;
    if (species.size !== 1) return [group, `Mixed species group ${group} (split required)`];
    const name = [...species][0];
    const number = (speciesCounts.get(name) ?? 0) + 1;
    speciesCounts.set(name, number);
    return [group, `${name ? name[0].toUpperCase() + name.slice(1) : "Unknown species"} ${number}`];
  }));
  const emptyGroup = detail?.instances.map((_, index) => String(index + 1)).find((group) => !groupSpecies.has(group));
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
        {detail.instances.length > 0 && <p className="hint">Evidence numbers are detections, not confirmed identities or an animal count. Select evidence below each photo to highlight its region, even when boxes overlap. Dog and cat numbers label groups only within this upload.</p>}
        {[...sources].map(([photoId, items], sourceIndex) => {
          const source = items.find(({ instance }) => instance.source_thumb_url && instance.source_bbox)?.instance;
          const timestamp = items[0].instance.timestamp_ms;
          return <section className="capture-source" key={photoId} aria-label={`Source photo ${sourceIndex + 1}`}>
            <h3>Source photo {sourceIndex + 1}{timestamp != null ? ` · ${(timestamp / 1000).toFixed(1)}s` : ""}</h3>
            {source ? <div className="capture-source-image">
              <img src={source.source_thumb_url} width={source.source_width} height={source.source_height} alt={`Full uncropped source photo ${sourceIndex + 1}`} />
              <div aria-hidden="true">{items.map(({ instance, index }) => instance.source_bbox && <span
                key={instance.id} id={`capture-box-${instance.id}`} className={`capture-box capture-source-box${selectedId === instance.id ? " is-selected" : ""}`}
                style={{ ...evidenceStyle(index), ...boxStyle(instance.source_bbox) }}>
                <span className="capture-box-number">{groupLabels.get(assignments[instance.id])} · Evidence {index + 1}</span>
              </span>)}</div>
            </div> : <p className="hint">Source overview unavailable. Showing individual evidence crops.</p>}
            <ul className="capture-evidence-grid">{items.map(({ instance, index }) => <li key={instance.id}>
              <article id={`capture-evidence-${instance.id}`} className={`capture-evidence-card${selectedId === instance.id ? " is-selected" : ""}`} style={evidenceStyle(index)} aria-label={`Evidence ${index + 1}`}>
                <button type="button" className="capture-evidence-select" aria-pressed={selectedId === instance.id}
                  aria-controls={instance.source_bbox ? `capture-box-${instance.id}` : undefined} onClick={() => setSelectedId(instance.id)}>
                  <span className="capture-evidence-number">{index + 1}</span>
                  <span>Evidence {index + 1} · {groupLabels.get(assignments[instance.id])}</span>
                  {selectedId === instance.id && <span className="capture-selection-label">Selected</span>}
                </button>
                <Evidence instance={instance} />
                <label>Group for evidence {index + 1}
                  <select aria-label={`Group for evidence ${index + 1}`} value={assignments[instance.id] ?? ""} disabled={saving || detail.processing_state === "ready"} onChange={(e) => {
                    const group = e.target.value;
                    const species = groupSpecies.get(group);
                    if (group !== emptyGroup && (species?.size !== 1 || !species.has(instance.species))) return;
                    if (group === emptyGroup) setAttrs((current) => ({ ...current, [group]: {} }));
                    setAssignments((current) => ({ ...current, [instance.id]: group }));
                  }}>
                    {groups.filter((group) => group === assignments[instance.id] || (groupSpecies.get(group)?.size === 1 && groupSpecies.get(group)?.has(instance.species))).map((group) => <option key={group} value={group} disabled={groupSpecies.get(group)?.size !== 1}>{groupLabels.get(group)}</option>)}
                    {emptyGroup && <option value={emptyGroup}>New {instance.species || "unknown species"} group</option>}
                  </select>
                </label>
              </article>
            </li>)}</ul>
          </section>;
        })}
        {invalid && <p role="alert">{invalid}</p>}
        <h3>Details for each animal</h3>
        <p className="hint" id="known-name-hint">Known names are optional and contributor-supplied. Names don’t confirm identity or merge animals. After a human confirms a match, the name is saved as a proposal in that animal’s naming history, not its official name.</p>
        {groups.map((group) => <fieldset key={group} disabled={saving} className={selectedId && assignments[selectedId] === group ? "capture-entry-selected" : undefined}>
          <legend>{groupLabels.get(group)}</legend>
          <label>Known name (optional)
            <input type="text" maxLength={80} value={attrs[group]?.known_name ?? ""} aria-describedby="known-name-hint"
              onChange={(e) => setAttrs((current) => ({ ...current, [group]: { ...current[group], known_name: e.target.value } }))}
              onBlur={(e) => setAttrs((current) => ({ ...current, [group]: { ...current[group], known_name: e.target.value.trim() || null } }))} />
          </label>
          <p className="hint">Evidence {detail.instances.flatMap((instance, index) => assignments[instance.id] === group ? [index + 1] : []).join(", ")}</p>
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
