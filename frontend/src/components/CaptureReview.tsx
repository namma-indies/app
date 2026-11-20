import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
import { decideSuggestion, deleteVoiceNote, notePending, redoTranscript, UnauthorizedError } from "../api";
import Transcript from "./Transcript";
import AddVoiceNote, { type LaterNotesConfig } from "./AddVoiceNote";
import { haptic } from "../haptics";
import { playFill, useSuggestionFlow } from "../suggestionFlow";
import { fxScope } from "../fxSprites";
import { getCapture, reviewCapture, type AnimalHint, type CaptureDetail, type CaptureGroup, type CaptureInstance, type CaptureSuggestion } from "../captureApi";
import { formatWhen } from "../formatWhen";
import { commonSpecies, isSpecies, speciesNoun, type Species } from "../species";
import ChipGroup from "./ChipGroup";
import { CONDITION_OPTIONS, EAR_NOTCH_OPTIONS, SEX_OPTIONS } from "../detailOptions";
import { useReloadGuard } from "../update/reloadGuard";

/** The choice for a box that shows no animal: a doubled box, or a detector
 * mistake. It is left out of every group and publishes nothing (#105). */
export const NOT_AN_ANIMAL = "none";

/** Two boxes of one photo are two animals, always; every box needs a place.
 * Species is the animal's, not the box's, so mixed per-box labels are fine. */
export function groupingError(instances: CaptureInstance[], assignments: Record<string, string>): string | null {
  if (instances.some((instance) => !assignments[instance.id])) return "Every box needs an animal.";
  for (let i = 0; i < instances.length; i++) {
    for (let j = i + 1; j < instances.length; j++) {
      const a = instances[i], b = instances[j];
      if (assignments[a.id] !== assignments[b.id] || assignments[a.id] === NOT_AN_ANIMAL) continue;
      if (a.photo_id === b.photo_id || a.co_visible_instance_ids?.includes(b.id) || b.co_visible_instance_ids?.includes(a.id)) {
        return "Two boxes in the same photo are two different animals, so they can’t share a group.";
      }
    }
  }
  return null;
}

/** A, B, … Z, AA, AB: an animal's letter follows it while boxes move around. */
export function animalLetter(key: string): string {
  let n = Number(key) - 1;
  let letter = "";
  do { letter = String.fromCharCode(65 + (n % 26)) + letter; n = Math.floor(n / 26) - 1; } while (n >= 0);
  return letter;
}

const FIELD_LABELS = { sex: "Sex", ear_notch: "Ear notch", condition: "Condition" } as const;
const SUGGESTION_LABELS = { ...FIELD_LABELS, known_name: "Name" } as const;
const CHIP_FIELDS = [["sex", SEX_OPTIONS], ["ear_notch", EAR_NOTCH_OPTIONS], ["condition", CONDITION_OPTIONS]] as const;
/** One colour per animal, the same on its card, its letter and its boxes in every photo. */
export const ANIMAL_COLORS = ["#ffda47", "#66d9ff", "#ff8cc6", "#a6ed83", "#c8a2ff", "#ffad66"];
const animalColor = (key: string) => key === NOT_AN_ANIMAL ? "#9a8a72" : ANIMAL_COLORS[(Number(key) - 1) % ANIMAL_COLORS.length];
const animalStyle = (key: string): CSSProperties => ({ "--evidence-color": animalColor(key) } as CSSProperties);
const boxStyle = (box: NonNullable<CaptureInstance["bbox"]>): CSSProperties => ({
  left: `${box[0] * 100}%`, top: `${box[1] * 100}%`, width: `${(box[2] - box[0]) * 100}%`, height: `${(box[3] - box[1]) * 100}%`,
});
const capital = (word: string) => word[0].toUpperCase() + word.slice(1);
const other = (species: Species): Species => species === "dog" ? "cat" : "dog";

function Evidence({ instance }: { instance: CaptureInstance }) {
  const box = instance.source_bbox ? undefined : instance.bbox;
  return <div className="capture-evidence">
    <img src={instance.source_url && box ? instance.source_url : instance.thumb_url} alt={`Box showing a ${speciesNoun(instance.species)}`} />
    {instance.source_url && box && <span className="capture-box" style={boxStyle(box)} />}
  </div>;
}

/** The whole photo(s) behind a one-animal sheet, each with the animal's box. Opened from the thumbnail. */
function Lightbox({ photos, onClose }: { photos: { instance: CaptureInstance; index: number }[][]; onClose: () => void }) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    closeRef.current?.focus({ preventScroll: true });
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.stopPropagation(); onClose(); return; }
      if (event.key !== "Tab") return;
      // Keep Tab inside the photo: nothing behind it (Save included) can be reached while it is open.
      const inside = [...dialogRef.current!.querySelectorAll<HTMLElement>("button, [href], input, select, [tabindex]:not([tabindex='-1'])")];
      event.preventDefault();
      const at = inside.indexOf(document.activeElement as HTMLElement);
      inside[(at + (event.shiftKey ? -1 : 1) + inside.length) % inside.length]?.focus({ preventScroll: true });
    };
    document.addEventListener("keydown", onKey, true);
    return () => { document.removeEventListener("keydown", onKey, true); opener?.focus?.({ preventScroll: true }); };
  }, [onClose]);
  return <div className="capture-lightbox" ref={dialogRef} role="dialog" aria-modal="true" aria-label="Photo with the animal boxed" onClick={onClose}>
    <div className="capture-lightbox-frame" onClick={(event) => event.stopPropagation()}>
    <div className="capture-lightbox-body" tabIndex={0} role="region" aria-label="Photos of this animal, scrollable">
      {photos.map((items, i) => {
        const source = items.find(({ instance }) => instance.source_thumb_url && instance.source_bbox)?.instance;
        return source
          ? <div className="capture-source-image" key={source.photo_id}>
            <img src={source.source_thumb_url} width={source.source_width} height={source.source_height} alt={`Whole photo ${i + 1}`} />
            <div aria-hidden="true">{items.map(({ instance }) => instance.source_bbox && <span key={instance.id} className="capture-box capture-source-box is-selected" style={boxStyle(instance.source_bbox)} />)}</div>
          </div>
          : <Evidence key={items[0].instance.id} instance={items[0].instance} />;
      })}
    </div>
    <button type="button" className="btn" ref={closeRef} onClick={onClose}>Close photo</button>
    </div>
  </div>;
}

export default function CaptureReview({ captureId, onClose, onSaved, onUnauthorized, laterNotes }: {
  captureId: string; onClose: () => void; onSaved: () => void; onUnauthorized: () => void;
  /** Set when "Add voice note" can be offered here (#156). */
  laterNotes?: LaterNotesConfig;
}) {
  const [detail, setDetail] = useState<CaptureDetail | null>(null);
  // Open for the whole review: assignments and attributes are unsaved until Save.
  useReloadGuard();
  const [assignments, setAssignments] = useState<Record<string, string>>({});
  const [attrs, setAttrs] = useState<Record<string, Omit<CaptureGroup, "instance_ids" | "species">>>({});
  // Each animal's species: the vote's (or a saved) answer until the person taps the other.
  const [species, setSpecies] = useState<Record<string, Species>>({});
  const [speciesSet, setSpeciesSet] = useState<Record<string, boolean>>({});
  // What the grouping said about each animal it suggested, by animal key.
  const [hints, setHints] = useState<Record<string, AnimalHint>>({});
  // The person changed the suggested grouping: no longer a one-tap "Looks right".
  const [regrouped, setRegrouped] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
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
    setRegrouped(false);
    setActionError(null);
    setSpeciesSet({});
    getCapture(captureId, controller.signal).then((result) => {
      if (controller.signal.aborted) return;
      setDetail(result);
      const tracks = new Map<string, string>();
      const savedAssignments: Record<string, string> = Object.fromEntries([
        ...result.groups.flatMap((group, index) => group.instance_ids.map((id) => [id, String(index + 1)])),
        ...(result.excluded_instance_ids ?? []).map((id) => [id, NOT_AN_ANIMAL]),
      ]);
      const next = Object.fromEntries(result.instances.map((instance) => {
        const key = instance.track_id ?? instance.id;
        if (!tracks.has(key)) tracks.set(key, String(result.groups.length + tracks.size + 1));
        return [instance.id, savedAssignments[instance.id] ?? tracks.get(key)!];
      }));
      setAssignments(next);
      setAttrs(Object.fromEntries(result.groups.map(({ instance_ids: _ids, species: _species, ...details }, index) => [String(index + 1), details])));
      const animalHints: Record<string, AnimalHint> = {};
      result.groups.forEach((group, index) => {
        const hint = result.animal_hints?.[index];
        if (hint && hint.instance_ids.length === group.instance_ids.length && hint.instance_ids.every((id) => group.instance_ids.includes(id))) animalHints[String(index + 1)] = hint;
      });
      setHints(animalHints);
      const kinds: Record<string, Species> = {};
      for (const key of new Set(Object.values(next))) {
        if (key === NOT_AN_ANIMAL) continue;
        const group = result.groups[Number(key) - 1];
        const members = result.instances.filter((instance) => next[instance.id] === key).map((instance) => instance.species);
        const chosen = group?.species ?? animalHints[key]?.species ?? commonSpecies(members) ?? members.find(isSpecies) ?? "dog";
        kinds[key] = isSpecies(chosen) ? chosen : "dog";
      }
      setSpecies(kinds);
    }).catch((err: unknown) => {
      if (controller.signal.aborted) return;
      if (err instanceof UnauthorizedError) unauthorizedRef.current();
      setError("Couldn’t load this upload.");
    });
    return () => controller.abort();
  }, [captureId, revision]);
  // A note added here, or still being worked on: check back for what was heard
  // and suggested, without reloading the sheet (that would lose unsaved edits).
  const [soundChecks, setSoundChecks] = useState(0);
  const notesPending = (detail?.later_notes ?? []).some(notePending)
    || (detail?.relistening ?? []).some((r) => (r.state ?? "listening") === "listening")
    || (detail?.voice_transcripts ?? []).some((t) => t.reading);
  const refreshSounds = useCallback(() => getCapture(captureId).then((fresh) => setDetail((current) => current && {
    ...current, voice_transcripts: fresh.voice_transcripts, later_notes: fresh.later_notes, relistening: fresh.relistening,
    suggestions: fresh.suggestions, answered_suggestions: fresh.answered_suggestions,
  })).catch((err: unknown) => {
    if (err instanceof UnauthorizedError) unauthorizedRef.current();
  }), [captureId]);
  useEffect(() => {
    if (!notesPending || soundChecks >= 12) return;
    const timer = setTimeout(() => { void refreshSounds().finally(() => setSoundChecks((n) => n + 1)); },
      Math.min(3000 * 2 ** soundChecks, 30000));
    return () => clearTimeout(timer);
  }, [notesPending, soundChecks, refreshSounds]);
  // Boxes in time order (a clip by its clock, a burst in upload order); a box's number is its place here.
  const ordered = (detail?.instances ?? []).map((instance, index) => ({ instance, index }))
    .sort((a, b) => (a.instance.timestamp_ms ?? -1) - (b.instance.timestamp_ms ?? -1) || a.index - b.index);
  const boxNumber = new Map(ordered.map(({ instance }, n) => [instance.id, n + 1]));
  const photoNumber = new Map<string, number>();
  ordered.forEach(({ instance }) => { if (!photoNumber.has(instance.photo_id)) photoNumber.set(instance.photo_id, photoNumber.size + 1); });
  const inPhoto = new Map<string, CaptureInstance[]>();
  ordered.forEach(({ instance }) => inPhoto.set(instance.photo_id, [...(inPhoto.get(instance.photo_id) ?? []), instance]));
  const groups = [...new Set(Object.values(assignments))].filter((group) => group !== NOT_AN_ANIMAL).sort((a, b) => Number(a) - Number(b));
  const membersOf = (group: string) => ordered.map(({ instance }) => instance).filter((instance) => assignments[instance.id] === group);
  const excluded = ordered.map(({ instance }) => instance).filter((instance) => assignments[instance.id] === NOT_AN_ANIMAL);
  const speciesOf = (group: string): Species => species[group] ?? commonSpecies(membersOf(group).map((i) => i.species)) ?? "dog";
  const labelOf = (group: string) => `${capital(speciesOf(group))} ${animalLetter(group)}`;
  const nextKey = () => String(Math.max(0, ...Object.values(assignments).filter((key) => key !== NOT_AN_ANIMAL).map(Number), detail?.instances.length ?? 0) + 1);
  const invalid = detail ? groupingError(detail.instances, assignments) : null;
  const locked = !detail || detail.processing_state === "ready" || saving;
  const representativeOf = (group: string) => {
    const members = membersOf(group);
    return members.find((instance) => instance.id === hints[group]?.representative_instance_id) ?? members[0];
  };
  /** Move one box: to another animal, to a new one, or out as not an animal. */
  function moveBox(instance: CaptureInstance, target: string) {
    setActionError(null);
    if (target === "new") {
      const key = nextKey();
      setSpecies((current) => ({ ...current, [key]: isSpecies(instance.species) ? instance.species : speciesOf(assignments[instance.id]) }));
      setAttrs((current) => ({ ...current, [key]: {} }));
      target = key;
    }
    setAssignments((current) => ({ ...current, [instance.id]: target }));
    setRegrouped(true);
    haptic("tick");
  }
  /** Merge one animal into another, unless they share a photo (then they are two). */
  function merge(from: string, into: string) {
    const shared = membersOf(from).find((a) => membersOf(into).some((b) => a.photo_id === b.photo_id));
    if (shared) {
      setActionError(`${labelOf(from)} and ${labelOf(into)} are both in photo ${photoNumber.get(shared.photo_id)}, so they’re two different animals.`);
      return;
    }
    setActionError(null);
    setAssignments((current) => Object.fromEntries(Object.entries(current).map(([id, key]) => [id, key === from ? into : key])));
    setRegrouped(true);
    haptic("tick");
  }
  function leaveOut(group: string) {
    setActionError(null);
    setAssignments((current) => Object.fromEntries(Object.entries(current).map(([id, key]) => [id, key === group ? NOT_AN_ANIMAL : key])));
    setRegrouped(true);
    haptic("tick");
  }
  function chooseSpecies(group: string, value: Species) {
    setSpecies((current) => ({ ...current, [group]: value }));
    setSpeciesSet((current) => ({ ...current, [group]: true }));
    haptic("tick");
  }
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
  /** Suggested values that came from the AI photo description, to mark as AI's (#160). */
  const photoChips = (group: string, field: string) => new Set(suggestionsFor(group)
    .filter((suggestion) => suggestion.field === field && suggestion.description_id).map((suggestion) => suggestion.value));
  const canSave = !!detail && (detail.processing_state === "needs_review" || detail.processing_state === "ready") && !error && !invalid && !saving && detail.instances.length > 0;
  async function save() {
    if (!detail || !canSave) return;
    if (!groups.length && !confirm("None of these boxes is an animal, so this upload will be discarded. It won’t appear anywhere.")) return;
    setSaving(true); setError(null);
    try {
      await reviewCapture(captureId, groups.map((group) => ({ ...attrs[group], species: speciesOf(group), instance_ids: membersOf(group).map((instance) => instance.id) })), detail.revision, excluded.map((instance) => instance.id));
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
  const compactPhotos = compact ? [...inPhoto.values()].map((items) => items.filter((instance) => assignments[instance.id] === groups[0]).map((instance) => ({ instance, index: boxNumber.get(instance.id)! - 1 }))).filter((items) => items.length) : [];
  const unsure = groups.filter((group) => hints[group]?.confident === false || (hints[group]?.species_uncertain && !speciesSet[group]));
  const animals = `${groups.length} ${groups.length === 1 ? "animal" : "animals"}`;
  // The pre-filled grouping, untouched: one tap publishes it as it is.
  const saveLabel = saving ? "Saving…" : detail?.processing_state === "ready" ? "Save animal details" : groups.length === 0 ? "Discard this upload"
    : regrouped ? `Publish ${animals}` : `Looks right · publish ${animals}`;
  // Until the capture is known there is nothing to say about it: not "ready", and not "needs grouping" either.
  const title = !detail ? "Loading…" : detail.processing_state === "ready" ? "Animal details" : "Check the animals";
  const nameHint = `A name doesn’t decide which ${speciesNoun(commonSpecies(groups.map(speciesOf)))} this is.`;
  const details = (group: string, thumb?: CaptureInstance) => <>
    <div className={thumb ? "capture-name-row" : undefined}>
      {thumb && <button type="button" className="capture-thumb" aria-label="Show the whole photo" onClick={() => setLightbox(true)}>
        <img src={thumb.thumb_url} alt={`A ${speciesOf(group)} in this upload`} />
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
    {CHIP_FIELDS.map(([field, options]) => <ChipGroup key={field} label={FIELD_LABELS[field]} field={field} options={[...options]} value={attrs[group]?.[field]}
      suggested={suggestedChips(group, field)} fromPhoto={photoChips(group, field)} filledBy={(value) => usedInto(group, field, value)} disabled={saving}
      onDismiss={suggestionsFor(group).some((suggestion) => suggestion.field === field) ? () => dismiss(group, field) : undefined}
      onChange={(value, chip) => choose(group, field, value, chip)} />)}
  </>;
  /** Where one box goes: any animal, a new one, or out. Two boxes of one photo can't share an animal. */
  const boxPicker = (instance: CaptureInstance) => {
    const n = boxNumber.get(instance.id);
    return <label className="animal-box-picker">Which animal is box {n}?
      <select aria-label={`Which animal is box ${n}?`} value={assignments[instance.id] ?? ""} disabled={locked}
        onChange={(e) => moveBox(instance, e.target.value)}>
        {groups.map((group) => <option key={group} value={group}
          disabled={group !== assignments[instance.id] && (inPhoto.get(instance.photo_id) ?? []).some((other) => other.id !== instance.id && assignments[other.id] === group)}>{labelOf(group)}</option>)}
        <option value="new">A new animal</option>
        <option value={NOT_AN_ANIMAL}>Not an animal (leave out)</option>
      </select>
    </label>;
  };
  /** One photo of an animal: the whole frame, every animal's box in its own colour, this one solid. */
  const frameView = (instance: CaptureInstance) => {
    const n = boxNumber.get(instance.id)!;
    const seconds = instance.timestamp_ms != null ? ` · ${(instance.timestamp_ms / 1000).toFixed(1)}s` : "";
    return <li key={instance.id} className="animal-frame" aria-label={`Box ${n}`}>
      {instance.source_thumb_url && instance.source_bbox ? <div className="capture-source-image">
        <img src={instance.source_thumb_url} width={instance.source_width} height={instance.source_height} alt={`Photo ${photoNumber.get(instance.photo_id)}, box ${n}`} />
        <div aria-hidden="true">{(inPhoto.get(instance.photo_id) ?? []).map((box) => box.source_bbox && <span key={box.id} id={box.id === instance.id ? `capture-box-${box.id}` : undefined}
          className={`capture-box capture-source-box${box.id === instance.id ? " is-selected" : ""}`} style={{ ...animalStyle(assignments[box.id] ?? NOT_AN_ANIMAL), ...boxStyle(box.source_bbox) }}>
          {box.id === instance.id && <span className="capture-box-number">{assignments[box.id] === NOT_AN_ANIMAL ? "–" : animalLetter(assignments[box.id])}</span>}
        </span>)}</div>
      </div> : <Evidence instance={instance} />}
      <p className="hint">Box {n} · photo {photoNumber.get(instance.photo_id)}{seconds}</p>
      {!locked && boxPicker(instance)}
    </li>;
  };
  const alerts = <>
    {invalid && <p role="alert">{invalid}</p>}
    {actionError && <p role="alert">{actionError}</p>}
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
          : detail && <p>{unsure.length
            ? `We sorted the photos into the animals we think we saw, but we’re not sure about ${unsure.length === 1 ? "one of them" : `${unsure.length} of them`}. Check the ${unsure.length === 1 ? "one" : "ones"} marked below, then publish.`
            : "We sorted the photos into the animals we think we saw. Check them, then publish."} Only you can see this upload until you do.</p>}
        <p className="sr-only" role="status">{announcement}</p>
        {!detail && error && <p role="alert">{error} <button disabled={saving} onClick={() => setRevision((n) => n + 1)}>Reload</button></p>}
        {!detail && !error && <p>Loading your photos…</p>}
        {detail && <>
          <p className="hint">Taken {formatWhen(detail.captured_at)}.{!compact && " Every animal from this upload shares this time and place."}</p>
          {detail.note && <p>Your note: {detail.note}</p>}
          <Transcript compact={compact} transcripts={detail.voice_transcripts ?? []} notes={detail.later_notes ?? []}
            // Removing your own recording never depends on whether adding one is possible now.
            onDeleteNote={async (id) => {
              try { await deleteVoiceNote(id); } catch (err) { if (err instanceof UnauthorizedError) unauthorizedRef.current(); throw err; }
              await refreshSounds();
            }} onRedo={async (id, language) => {
              try {
                await redoTranscript(id, language);
              } catch (err) {
                if (err instanceof UnauthorizedError) unauthorizedRef.current();
                throw err;
              } finally {
                // A redo whose answer was lost may still have gone through.
                setSoundChecks(0);
                await refreshSounds();
              }
            }} relistening={detail.relistening ?? []} highlights={[
            ...stillOpen.filter((suggestion) => !answers[suggestion.id] && !suggestion.description_id)
              .map((suggestion) => ({ id: suggestion.id, quote: suggestion.quote, label: `${SUGGESTION_LABELS[suggestion.field]}: ${suggestion.value}`, noteId: suggestion.note_id })),
            // Used, in an earlier visit or just now: still marked, quieter. Turned down ("not this") is wrong, so it isn't.
            ...[...alreadyUsed, ...(detail.suggestions ?? []).filter((suggestion) => answers[suggestion.id] === "used")]
              .filter((suggestion) => !suggestion.description_id)
              .map((suggestion) => ({ id: suggestion.id, quote: suggestion.quote, label: `${SUGGESTION_LABELS[suggestion.field]}: ${suggestion.value}`, used: true, noteId: suggestion.note_id })),
          ]} />
          {laterNotes && <AddVoiceNote target={{ captureId }} max={laterNotes.max}
            added={(detail.later_notes ?? []).filter((note) => !note.sighting_id).length}
            onAdded={() => { setSoundChecks(0); void refreshSounds(); }} onUnauthorized={onUnauthorized} />}
          {!compact && <p className="hint" id="known-name-hint">{nameHint}</p>}
          {compact
            ? <fieldset disabled={saving} {...fxScope(speciesOf(groups[0]))} className="capture-entry-compact">
              <legend className="sr-only">{labelOf(groups[0])}</legend>
              {details(groups[0], representativeOf(groups[0]))}
            </fieldset>
            : groups.map((group) => {
              const members = membersOf(group);
              const face = representativeOf(group);
              const hint = hints[group];
              const kind = speciesOf(group);
              const label = labelOf(group);
              const groupingUnsure = hint?.confident === false;
              const speciesUnsure = !!hint?.species_uncertain && !speciesSet[group];
              return <article key={group} className={`animal-card${groupingUnsure || speciesUnsure ? " is-unsure" : ""}`} style={animalStyle(group)} aria-label={label}>
                <header className="animal-card-head">
                  <span className="animal-letter" aria-hidden="true">{animalLetter(group)}</span>
                  {face && <img className="animal-face" src={face.thumb_url} alt={`${label}, their clearest photo`} />}
                  <div className="animal-card-title">
                    <h3>{label}</h3>
                    <p className="hint">{members.length === 1 ? "In 1 photo" : `In ${members.length} photos`}</p>
                    <div className="animal-species" role="radiogroup" aria-label={`Is ${animalLetter(group)} a dog or a cat?`}>
                      {(["dog", "cat"] as const).map((value) => <button key={value} type="button" role="radio" aria-checked={kind === value}
                        className={`chip chip-choice${kind === value ? " is-chosen" : ""}`} disabled={locked} onClick={() => chooseSpecies(group, value)}>{capital(value)}</button>)}
                    </div>
                  </div>
                </header>
                {speciesUnsure && <p className="animal-hint">{label} might be a {other(kind)}. Tap {capital(other(kind))} if so.</p>}
                {groupingUnsure && !regrouped && <p className="animal-hint">We’re not sure every photo here shows the same {kind}. Open the photos and move any that show someone else.</p>}
                <details className="animal-frames" open={groupingUnsure && !regrouped}>
                  <summary>{members.length === 1 ? "The photo of them" : `All ${members.length} photos of them`}</summary>
                  <ol className="animal-strip">{members.map(frameView)}</ol>
                </details>
                {!locked && <div className="animal-actions">
                  {groups.length > 1 && <label>Same {kind} as…
                    <select aria-label={`${label} is the same animal as`} value="" onChange={(e) => e.target.value && merge(group, e.target.value)}>
                      <option value="">Choose an animal</option>
                      {groups.filter((target) => target !== group).map((target) => <option key={target} value={target}>{labelOf(target)}</option>)}
                    </select>
                  </label>}
                  <button type="button" className="link-btn" aria-label={`${label} isn’t an animal: leave them out`} onClick={() => leaveOut(group)}>Not an animal · leave out</button>
                </div>}
                <fieldset disabled={saving} {...fxScope(kind)}>
                  <legend>{label}</legend>
                  {details(group)}
                </fieldset>
              </article>;
            })}
          {!compact && excluded.length > 0 && <section className="animal-left-out" aria-label="Left out">
            <h3>Left out · not an animal</h3>
            <ol className="animal-strip">{excluded.map(frameView)}</ol>
          </section>}
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
