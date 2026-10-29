import { useEffect, useRef, useState, type CSSProperties } from "react";
import { decideSuggestion, UnauthorizedError } from "../api";
import Transcript from "./Transcript";
import { haptic } from "../haptics";
import { playFill, useSuggestionFlow } from "../suggestionFlow";
import { getCapture, reviewCapture, type CaptureDetail, type CaptureGroup, type CaptureInstance, type CaptureSuggestion } from "../captureApi";
import { formatWhen } from "../formatWhen";
import ChipGroup from "./ChipGroup";
import { CONDITION_OPTIONS, EAR_NOTCH_OPTIONS, SEX_OPTIONS } from "../detailOptions";

/** The choice for a box that shows no animal: a doubled box, or a detector
 * mistake. It is left out of every group and publishes nothing (#105). */
export const NOT_AN_ANIMAL = "none";

export function groupingError(instances: CaptureInstance[], assignments: Record<string, string>): string | null {
  if (instances.some((instance) => !assignments[instance.id])) return "Every box needs an animal.";
  for (let i = 0; i < instances.length; i++) {
    for (let j = i + 1; j < instances.length; j++) {
      const a = instances[i], b = instances[j];
      if (assignments[a.id] !== assignments[b.id] || assignments[a.id] === NOT_AN_ANIMAL) continue;
      if (a.photo_id === b.photo_id || a.co_visible_instance_ids?.includes(b.id) || b.co_visible_instance_ids?.includes(a.id)) {
        return "Two boxes in the same photo are two different animals, so they can’t share a group.";
      }
      if (a.species !== b.species) return "Different species can’t share a group.";
    }
  }
  return null;
}

const FIELD_LABELS = { sex: "Sex", ear_notch: "Ear notch", condition: "Condition" } as const;
const SUGGESTION_LABELS = { ...FIELD_LABELS, known_name: "Name" } as const;
const CHIP_FIELDS = [["sex", SEX_OPTIONS], ["ear_notch", EAR_NOTCH_OPTIONS], ["condition", CONDITION_OPTIONS]] as const;
const evidenceColors = ["#ffda47", "#66d9ff", "#ff8cc6", "#a6ed83", "#c8a2ff", "#ffad66"];
const evidenceStyle = (index: number): CSSProperties => ({ "--evidence-color": evidenceColors[index % evidenceColors.length] } as CSSProperties);
const boxStyle = (box: NonNullable<CaptureInstance["bbox"]>): CSSProperties => ({
  left: `${box[0] * 100}%`, top: `${box[1] * 100}%`, width: `${(box[2] - box[0]) * 100}%`, height: `${(box[3] - box[1]) * 100}%`,
});

function Evidence({ instance }: { instance: CaptureInstance }) {
  const box = instance.source_bbox ? undefined : instance.bbox;
  return <div className="capture-evidence">
    <img src={instance.source_url && box ? instance.source_url : instance.thumb_url} alt={`Box showing a ${instance.species}`} />
    {instance.source_url && box && <span className="capture-box" style={{ left: `${box[0] * 100}%`, top: `${box[1] * 100}%`, width: `${(box[2] - box[0]) * 100}%`, height: `${(box[3] - box[1]) * 100}%` }} />}
  </div>;
}

/** The whole photo(s) behind a one-animal sheet, each with the animal's box. Opened from the thumbnail. */
function Lightbox({ photos, onClose }: { photos: { instance: CaptureInstance; index: number }[][]; onClose: () => void }) {
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") { event.stopPropagation(); onClose(); } };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [onClose]);
  return <div className="capture-lightbox" role="dialog" aria-modal="true" aria-label="Photo with the animal boxed" onClick={onClose}>
    <div className="capture-lightbox-body" onClick={(event) => event.stopPropagation()}>
      {photos.map((items, i) => {
        const source = items.find(({ instance }) => instance.source_thumb_url && instance.source_bbox)?.instance;
        return source
          ? <div className="capture-source-image" key={source.photo_id}>
            <img src={source.source_thumb_url} width={source.source_width} height={source.source_height} alt={`Whole photo ${i + 1}`} />
            <div aria-hidden="true">{items.map(({ instance }) => instance.source_bbox && <span key={instance.id} className="capture-box capture-source-box is-selected" style={boxStyle(instance.source_bbox)} />)}</div>
          </div>
          : <Evidence key={items[0].instance.id} instance={items[0].instance} />;
      })}
      <button type="button" className="btn" ref={closeRef} onClick={onClose}>Close photo</button>
    </div>
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
  // Voice-note suggestions the observer has answered in this sheet. Nothing is
  // decided until the sheet saves: "used" only fills the field in.
  const [answers, setAnswers] = useState<Record<string, "used" | "dismissed">>({});
  // Said aloud by a screen reader when a suggestion fills a field; the glow that
  // goes with it is decoration.
  const [announcement, setAnnouncement] = useState("");
  const [lightbox, setLightbox] = useState(false);
  const sheetRef = useRef<HTMLElement>(null);
  const alreadyUsed = (detail?.answered_suggestions ?? []).filter((suggestion) => suggestion.status === "accepted" || suggestion.status === "filled");
  // The raw open list also holds one whose field was filled in by hand with its value (`filled`):
  // that is used, not open, so it must not be treated as both.
  const stillOpen = (detail?.suggestions ?? []).filter((suggestion) => !alreadyUsed.some((used) => used.id === suggestion.id));
  useSuggestionFlow(sheetRef, stillOpen.map((suggestion) => suggestion.id), alreadyUsed.map((suggestion) => suggestion.id));
  const unauthorizedRef = useRef(onUnauthorized);
  unauthorizedRef.current = onUnauthorized;
  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    setDetail(null);
    setAnswers({});
    getCapture(captureId, controller.signal).then((result) => {
      if (controller.signal.aborted) return;
      setDetail(result);
      setSelectedId(result.instances[0]?.id ?? null);
      const tracks = new Map<string, string>();
      const savedAssignments: Record<string, string> = Object.fromEntries([
        ...result.groups.flatMap((group, index) => group.instance_ids.map((id) => [id, String(index + 1)])),
        ...(result.excluded_instance_ids ?? []).map((id) => [id, NOT_AN_ANIMAL]),
      ]);
      setAttrs(Object.fromEntries(result.groups.map(({ instance_ids: _ids, ...details }, index) => [String(index + 1), details])));
      setAssignments(Object.fromEntries(result.instances.map((instance) => {
        const key = instance.track_id ?? instance.id;
        if (!tracks.has(key)) tracks.set(key, String(tracks.size + 1));
        return [instance.id, savedAssignments[instance.id] ?? tracks.get(key)!];
      })));
    }).catch((err: unknown) => {
      if (controller.signal.aborted) return;
      if (err instanceof UnauthorizedError) unauthorizedRef.current();
      setError("Couldn’t load this upload.");
    });
    return () => controller.abort();
  }, [captureId, revision]);
  const sources = new Map<string, { instance: CaptureInstance; index: number }[]>();
  detail?.instances.forEach((instance, index) => {
    const items = sources.get(instance.photo_id) ?? [];
    items.push({ instance, index });
    sources.set(instance.photo_id, items);
  });
  const groups = [...new Set(Object.values(assignments))].filter((group) => group !== NOT_AN_ANIMAL).sort((a, b) => Number(a) - Number(b));
  const excluded = detail?.instances.filter((instance) => assignments[instance.id] === NOT_AN_ANIMAL).map((instance) => instance.id) ?? [];
  const boxLabel = (instance: CaptureInstance) => assignments[instance.id] === NOT_AN_ANIMAL ? "Not an animal" : groupLabels.get(assignments[instance.id]);
  const speciesCounts = new Map<string, number>();
  const groupSpecies = new Map(groups.map((group) => [group, new Set(detail?.instances.filter((instance) => assignments[instance.id] === group).map((instance) => instance.species))]));
  const groupLabels = new Map(groups.map((group) => {
    const species = groupSpecies.get(group)!;
    if (species.size !== 1) return [group, `Mixed group ${group} (needs splitting)`];
    const name = [...species][0];
    const number = (speciesCounts.get(name) ?? 0) + 1;
    speciesCounts.set(name, number);
    return [group, `${name ? name[0].toUpperCase() + name.slice(1) : "Unknown species"} ${number}`];
  }));
  const emptyGroup = detail?.instances.map((_, index) => String(index + 1)).find((group) => !groupSpecies.has(group));
  const invalid = detail ? groupingError(detail.instances, assignments) : null;
  // Offered under whichever animal its box is now grouped into, while that
  // detail is still blank there and the observer hasn't answered it.
  const suggestionsFor = (group: string): CaptureSuggestion[] => (detail?.suggestions ?? []).filter((suggestion) =>
    assignments[suggestion.instance_id] === group && !answers[suggestion.id] && !attrs[group]?.[suggestion.field]);
  // Where a used suggestion's words flow: the field it filled, under the animal its box is grouped into.
  // (For a chip, only the one still holding the value that was filled in.)
  const usedInto = (group: string, field: string, value?: string) => [...alreadyUsed, ...(detail?.suggestions ?? []).filter((suggestion) => answers[suggestion.id] === "used")]
    .filter((suggestion) => suggestion.field === field && assignments[suggestion.instance_id] === group && (value === undefined || (suggestion.value === value && attrs[group]?.[field] === value)))
    .map((suggestion) => suggestion.id).join(" ") || undefined;
  /** Set a detail from a chip. A chip that is a suggestion's value uses the suggestion: its glow lands on this very chip. */
  function choose(group: string, field: "sex" | "ear_notch" | "condition", value: string | undefined, chip: HTMLElement) {
    const used = value ? suggestionsFor(group).filter((suggestion) => suggestion.field === field && suggestion.value === value) : [];
    if (used.length) {
      playFill(null, chip);
      haptic("filled");
      setAnnouncement(`Filled ${FIELD_LABELS[field].toLowerCase()} with ${value}.`);
      setAnswers((current) => ({ ...current, ...Object.fromEntries(used.map((suggestion) => [suggestion.id, "used" as const])) }));
    } else haptic("tick"); // picking or clearing a chip by hand: a light tap

    setAttrs((current) => ({ ...current, [group]: { ...current[group], [field]: value } }));
  }
  const dismiss = (group: string, field: string) => setAnswers((current) => ({ ...current, ...Object.fromEntries(suggestionsFor(group).filter((suggestion) => suggestion.field === field).map((suggestion) => [suggestion.id, "dismissed" as const])) }));
  /** The chips (or the name) a suggestion would fill, by value. */
  const suggestedChips = (group: string, field: string) => {
    const byValue: Record<string, string[]> = {};
    suggestionsFor(group).filter((suggestion) => suggestion.field === field).forEach((suggestion) => { (byValue[suggestion.value] ??= []).push(suggestion.id); });
    return byValue;
  };
  const canSave = !!detail && (detail.processing_state === "needs_review" || detail.processing_state === "ready") && !error && !invalid && !saving && detail.instances.length > 0;
  async function save() {
    if (!detail || !canSave) return;
    if (!groups.length && !confirm("None of these boxes is an animal, so this upload will be discarded. It won’t appear anywhere.")) return;
    setSaving(true); setError(null);
    try {
      await reviewCapture(captureId, groups.map((group) => ({ ...attrs[group], instance_ids: detail.instances.filter((instance) => assignments[instance.id] === group).map((instance) => instance.id) })), detail.revision, excluded);
      // Bookkeeping after the save that actually decided: what was finally
      // chosen for a suggestion that was used, or that it was turned down.
      // Best effort -- a lost record must not undo or block the save.
      await Promise.allSettled((detail.suggestions ?? []).flatMap((suggestion) => {
        const answer = answers[suggestion.id];
        if (!answer) return [];
        const chosen = attrs[assignments[suggestion.instance_id]]?.[suggestion.field];
        return [decideSuggestion(suggestion.id, answer === "used" && chosen ? { verdict: "choose", value: chosen } : { verdict: "dismiss" })];
      }));
      haptic("logged"); // only here: the server took the save, a failure stays quiet
      onSaved();
    } catch (err) {
      if (err instanceof UnauthorizedError) unauthorizedRef.current();
      setError("Couldn’t tell whether that saved. Reload to check before trying again.");
    } finally { setSaving(false); }
  }
  // One animal, its boxes already settled: the sheet is just the details, with a thumbnail instead of the photos.
  const compact = detail?.processing_state === "ready" && groups.length === 1;
  const compactPhotos = compact ? [...sources.values()].map((items) => items.filter(({ instance }) => assignments[instance.id] === groups[0])).filter((items) => items.length) : [];
  const saveLabel = saving ? "Saving…" : detail?.processing_state === "ready" ? "Save animal details" : groups.length === 0 ? "Discard this upload" : `Publish ${groups.length} ${groups.length === 1 ? "animal" : "animals"}`;
  const title = detail?.processing_state === "ready" ? "Animal details" : "Separate the animals";
  const nameHint = "A name doesn’t decide which dog this is.";
  const details = (group: string, thumb?: CaptureInstance) => <>
    <div className={thumb ? "capture-name-row" : undefined}>
      {thumb && <button type="button" className="capture-thumb" aria-label="Show the whole photo" onClick={() => setLightbox(true)}>
        <img src={thumb.thumb_url} alt={`A ${thumb.species || "animal"} in this upload`} />
      </button>}
      <label>Known name (optional)
        <input type="text" data-field="known_name" data-fx-into={usedInto(group, "known_name")} maxLength={80} value={attrs[group]?.known_name ?? ""} aria-describedby="known-name-hint"
          onChange={(e) => setAttrs((current) => ({ ...current, [group]: { ...current[group], known_name: e.target.value } }))}
          onBlur={(e) => setAttrs((current) => ({ ...current, [group]: { ...current[group], known_name: e.target.value.trim() || null } }))} />
      </label>
    </div>
    {compact && <p className="hint" id="known-name-hint">{nameHint}</p>}
    {suggestionsFor(group).filter((suggestion) => suggestion.field === "known_name").map((suggestion) => <p className="voice-suggestion" key={suggestion.id}>
      Heard: <button type="button" className="chip suggested chip-choice" data-fx-chip={suggestion.id} aria-label={`Use ${suggestion.value} for name`} onClick={(event) => {
        playFill(event.currentTarget, event.currentTarget.closest("fieldset")?.querySelector<HTMLElement>('[data-field="known_name"]') ?? null);
        haptic("filled");
        setAnnouncement(`Filled name with ${suggestion.value}.`);
        setAttrs((current) => ({ ...current, [group]: { ...current[group], known_name: suggestion.value } }));
        setAnswers((current) => ({ ...current, [suggestion.id]: "used" }));
      }}>{suggestion.value}</button>{" "}
      <button type="button" className="link-btn" onClick={() => setAnswers((current) => ({ ...current, [suggestion.id]: "dismissed" }))}>not this</button>
    </p>)}
    {!compact && groups.length > 1 && <p className="hint">Box {detail!.instances.flatMap((instance, index) => assignments[instance.id] === group ? [index + 1] : []).join(", ")}</p>}
    {CHIP_FIELDS.map(([field, options]) => <ChipGroup key={field} label={FIELD_LABELS[field]} field={field} options={[...options]} value={attrs[group]?.[field]}
      suggested={suggestedChips(group, field)} filledBy={(value) => usedInto(group, field, value)} disabled={saving}
      onDismiss={suggestionsFor(group).some((suggestion) => suggestion.field === field) ? () => dismiss(group, field) : undefined}
      onChange={(value, chip) => choose(group, field, value, chip)} />)}
  </>;
  const alerts = <>
    {invalid && <p role="alert">{invalid}</p>}
    {error && <p role="alert">{error} <button disabled={saving} onClick={() => setRevision((n) => n + 1)}>Reload</button></p>}
  </>;
  return <div className="capture-review-overlay" role="dialog" aria-modal="true" aria-label={title}>
    <section className="capture-review" ref={sheetRef}>
      <div className="capture-review-body">
        <header className="capture-review-head">
          <h2>{title}</h2>
          <button className="link-btn" onClick={onClose} disabled={saving}>Close</button>
        </header>
        {detail?.processing_state === "ready"
          ? <p className="hint">Add what you know. You can change it any time.</p>
          : <p>More than one animal shows up at the same moment in this upload, so we can’t tell on our own which photos show which. Each box below is one animal we found. Put every box of the same animal in the same group, then publish. Only you can see this upload until you do.</p>}
        <p className="sr-only" role="status">{announcement}</p>
        {!detail && error && <p role="alert">{error} <button disabled={saving} onClick={() => setRevision((n) => n + 1)}>Reload</button></p>}
        {!detail && !error && <p>Loading your photos…</p>}
        {detail && <>
          <p className="hint">Taken {formatWhen(detail.captured_at)}.{!compact && " Every animal from this upload shares this time and place."}</p>
          {detail.note && <p>Your note: {detail.note}</p>}
          <Transcript compact={compact} transcripts={detail.voice_transcripts ?? []} highlights={[
            ...stillOpen.filter((suggestion) => !answers[suggestion.id])
              .map((suggestion) => ({ id: suggestion.id, quote: suggestion.quote, label: `${SUGGESTION_LABELS[suggestion.field]}: ${suggestion.value}` })),
            // Used, in an earlier visit or just now: still marked, quieter. Turned down ("not this") is wrong, so it isn't.
            ...[...alreadyUsed, ...(detail.suggestions ?? []).filter((suggestion) => answers[suggestion.id] === "used")]
              .map((suggestion) => ({ id: suggestion.id, quote: suggestion.quote, label: `${SUGGESTION_LABELS[suggestion.field]}: ${suggestion.value}`, used: true })),
          ]} />
          {!compact && detail.instances.length > 0 && <p className="hint">Tap a box to find it in the photo. The numbers only tell boxes apart.</p>}
          {!compact && [...sources].map(([photoId, items], sourceIndex) => {
            const source = items.find(({ instance }) => instance.source_thumb_url && instance.source_bbox)?.instance;
            const timestamp = items[0].instance.timestamp_ms;
            return <section className="capture-source" key={photoId} aria-label={`Photo ${sourceIndex + 1}`}>
              <h3>Photo {sourceIndex + 1}{timestamp != null ? ` · ${(timestamp / 1000).toFixed(1)}s` : ""}</h3>
              {source ? <div className="capture-source-image">
                <img src={source.source_thumb_url} width={source.source_width} height={source.source_height} alt={`Whole photo ${sourceIndex + 1}`} />
                <div aria-hidden="true">{items.map(({ instance, index }) => instance.source_bbox && <span
                  key={instance.id} id={`capture-box-${instance.id}`} className={`capture-box capture-source-box${selectedId === instance.id ? " is-selected" : ""}`}
                  style={{ ...evidenceStyle(index), ...boxStyle(instance.source_bbox) }}>
                  <span className="capture-box-number">{boxLabel(instance)} · Box {index + 1}</span>
                </span>)}</div>
              </div> : <p className="hint">Couldn’t show the whole photo, so here are the boxes on their own.</p>}
              <ul className="capture-evidence-grid">{items.map(({ instance, index }) => <li key={instance.id}>
                <article id={`capture-evidence-${instance.id}`} className={`capture-evidence-card${selectedId === instance.id ? " is-selected" : ""}${assignments[instance.id] === NOT_AN_ANIMAL ? " is-excluded" : ""}`} style={evidenceStyle(index)} aria-label={`Box ${index + 1}`}>
                  <button type="button" className="capture-evidence-select" aria-pressed={selectedId === instance.id}
                    aria-controls={instance.source_bbox ? `capture-box-${instance.id}` : undefined} onClick={() => setSelectedId(instance.id)}>
                    <span className="capture-evidence-number">{index + 1}</span>
                    <span>Box {index + 1} · {boxLabel(instance)}</span>
                    {selectedId === instance.id && <span className="capture-selection-label">Selected</span>}
                  </button>
                  <Evidence instance={instance} />
                  <label>Which animal is box {index + 1}?
                    <select aria-label={`Which animal is box ${index + 1}?`} value={assignments[instance.id] ?? ""} disabled={saving || detail.processing_state === "ready"} onChange={(e) => {
                      const group = e.target.value;
                      const species = groupSpecies.get(group);
                      if (group !== emptyGroup && group !== NOT_AN_ANIMAL && (species?.size !== 1 || !species.has(instance.species))) return;
                      if (group === emptyGroup) setAttrs((current) => ({ ...current, [group]: {} }));
                      setAssignments((current) => ({ ...current, [instance.id]: group }));
                    }}>
                      {groups.filter((group) => group === assignments[instance.id] || (groupSpecies.get(group)?.size === 1 && groupSpecies.get(group)?.has(instance.species))).map((group) => <option key={group} value={group} disabled={groupSpecies.get(group)?.size !== 1}>{groupLabels.get(group)}</option>)}
                      {emptyGroup && <option value={emptyGroup}>A new {instance.species || "animal"}</option>}
                      <option value={NOT_AN_ANIMAL}>Not an animal (leave out)</option>
                    </select>
                  </label>
                </article>
              </li>)}</ul>
            </section>;
          })}
          {!compact && <h3>Details for each animal</h3>}
          {!compact && <p className="hint" id="known-name-hint">{nameHint}</p>}
          {groups.map((group) => <fieldset key={group} disabled={saving} className={compact ? "capture-entry-compact" : selectedId && assignments[selectedId] === group ? "capture-entry-selected" : undefined}>
            <legend className={compact ? "sr-only" : undefined}>{groupLabels.get(group)}</legend>
            {details(group, compact ? compactPhotos[0]?.[0]?.instance : undefined)}
          </fieldset>)}
        </>}
      </div>
      {detail && <div className="capture-review-footer">
        {alerts}
        <button className="btn btn-primary" disabled={!canSave} onClick={() => void save()}>{saveLabel}</button>
      </div>}
      {lightbox && compact && <Lightbox photos={compactPhotos} onClose={() => setLightbox(false)} />}
    </section>
  </div>;
}
