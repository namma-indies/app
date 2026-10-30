import { useState } from "react";
import { decideSuggestion, UnauthorizedError, type Suggestion, type SuggestedField } from "../api";
import { haptic } from "../haptics";
import { CONDITION_OPTIONS, EAR_NOTCH_OPTIONS, SEX_OPTIONS } from "../detailOptions";

const LABELS: Record<SuggestedField, string> = {
  sex: "sex",
  ear_notch: "ear notch",
  condition: "condition",
  known_name: "name",
};

function optionsFor(s: Suggestion): { value: string; label: string }[] {
  if (s.field === "sex") return SEX_OPTIONS;
  if (s.field === "ear_notch") return EAR_NOTCH_OPTIONS;
  if (s.field === "condition") return CONDITION_OPTIONS;
  return [{ value: s.value, label: s.value }];
}

/**
 * Details read from the observer's voice note (or, for a sighting with no
 * note, from the AI photo description: grouped apart and marked AI), as blanks
 * to fill in.
 *
 * Each shows the usual options with the suggested one marked and the words it
 * came from underneath. Nothing is saved until a person taps an option: the
 * suggested one, a different one, or "not this". A name has a single option
 * (the name heard) because there is nothing to choose between.
 */
export default function SuggestedDetails({
  suggestions,
  onDecided,
  onUnauthorized,
}: {
  suggestions: Suggestion[];
  onDecided: () => void;
  onUnauthorized: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  // Hidden as soon as it is answered, before the /dex refresh comes back.
  const [answered, setAnswered] = useState<Set<string>>(new Set());
  const open = suggestions.filter((s) => !answered.has(s.id));
  if (open.length === 0) return null;

  async function decide(s: Suggestion, value: string | null) {
    setBusy(s.id);
    setFailed(false);
    try {
      await decideSuggestion(s.id, value === null ? { verdict: "dismiss" } : { verdict: "choose", value });
      setAnswered((cur) => new Set(cur).add(s.id));
      if (value !== null) haptic("filled");
      onDecided();
    } catch (err) {
      if (err instanceof UnauthorizedError) onUnauthorized();
      else setFailed(true);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="suggested-details">
      {[open.filter((s) => !s.description_id), open.filter((s) => s.description_id)].map((group) => group.length > 0 && <div key={group[0].description_id ? "photo" : "voice"}>
      {/* A photo suggestion is the model's reading of a picture, so it says so (#160). */}
      <p className="spot-label">{group[0].description_id ? "FROM THE PHOTO · AI" : "FROM YOUR VOICE NOTE"}</p>
      {group.map((s) => (
        <div className="field-group suggested-field" key={s.id}>
          <label>{LABELS[s.field]}</label>
          <div className="chip-row">
            {optionsFor(s).map((o) => (
              <button
                key={o.value}
                type="button"
                className={"chip" + (o.value === s.value ? " suggested" : "")}
                disabled={busy === s.id}
                aria-label={o.value === s.value ? `${o.label} (suggested)` : o.label}
                data-fx-chip={o.value === s.value ? s.id : undefined}
                onClick={() => decide(s, o.value)}
              >
                {o.label}
              </button>
            ))}
            <button
              type="button"
              className="link-btn"
              disabled={busy === s.id}
              onClick={() => decide(s, null)}
            >
              not this
            </button>
          </div>
          <p className="hint suggestion-quote">“{s.quote}”</p>
        </div>
      ))}
      </div>)}
      {failed && <p className="hint place-warning">Couldn't save that. Try again.</p>}
    </div>
  );
}
