import type { KeyboardEvent } from "react";

export interface ChipOption { value: string; label: string }

/**
 * One detail as a row of one-tap chips: a radiogroup that can also be left
 * blank. Tapping a chip sets it; tapping the chosen one clears it back to "not
 * recorded". Arrow keys move through the chips and choose as they go, like
 * native radios; Space/Enter on the chosen chip clears it.
 *
 * `suggested` maps a value to the voice-note suggestion ids it would fill
 * (data-fx-chip: where the transcript words land); `filledBy` does the same for
 * an answered one (data-fx-into: where the glow lands).
 */
export default function ChipGroup({ label, field, options, value, onChange, suggested, fromPhoto, filledBy, onDismiss, disabled }: {
  label: string;
  field: string;
  options: ChipOption[];
  value: string | null | undefined;
  onChange: (value: string | undefined, chip: HTMLElement) => void;
  suggested?: Record<string, string[]>;
  /** Suggested values read from the AI photo description, not a note: marked AI (#160). */
  fromPhoto?: Set<string>;
  filledBy?: (value: string) => string | undefined;
  /** Shown beside the label while a suggestion is open for this field. */
  onDismiss?: () => void;
  disabled?: boolean;
}) {
  const labelId = `chips-${field}-${label}`.replace(/\s+/g, "-");
  const tabbable = options.some((o) => o.value === value) ? value : options[0]?.value;
  function onKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    const step = event.key === "ArrowRight" || event.key === "ArrowDown" ? 1 : event.key === "ArrowLeft" || event.key === "ArrowUp" ? -1 : 0;
    if (!step) return;
    event.preventDefault();
    const next = (index + step + options.length) % options.length;
    const chips = event.currentTarget.parentElement!.querySelectorAll<HTMLElement>('[role="radio"]');
    chips[next].focus();
    onChange(options[next].value, chips[next]);
  }
  return <div className="chip-field">
    <div className="chip-field-head">
      <span className="chip-field-label" id={labelId}>{label}</span>
      {onDismiss && <button type="button" className="link-btn" disabled={disabled} aria-label={`Not this suggestion for ${label.toLowerCase()}`} onClick={onDismiss}>not this</button>}
    </div>
    <div className="chip-row" role="radiogroup" aria-labelledby={labelId}>
      {options.map((option, index) => {
        const checked = option.value === value;
        const ids = suggested?.[option.value];
        return <button key={option.value} type="button" role="radio" aria-checked={checked} disabled={disabled}
          tabIndex={option.value === tabbable ? 0 : -1}
          className={`chip chip-choice${checked ? " active" : ""}${ids?.length && !checked ? " suggested" : ""}`}
          data-field={field} data-value={option.value}
          data-fx-chip={ids?.length ? ids.join(" ") : undefined}
          data-fx-into={checked ? filledBy?.(option.value) : undefined}
          onKeyDown={(event) => onKeyDown(event, index)}
          onClick={(event) => onChange(checked ? undefined : option.value, event.currentTarget)}>
          {option.label}{ids?.length && !checked ? (fromPhoto?.has(option.value)
            ? <><span className="chip-ai" aria-hidden="true">AI</span><span className="sr-only"> (suggested by AI from the photo)</span></>
            : <span className="sr-only"> (suggested from your voice note)</span>) : null}
        </button>;
      })}
    </div>
  </div>;
}
