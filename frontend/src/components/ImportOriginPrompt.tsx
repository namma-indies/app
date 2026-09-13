import { useState } from "react";
import type { PhotoMetadata } from "../api";
import {
  originFromExif,
  originFromPerson,
  resolveCapturedAt,
  type ImportOrigin,
} from "../capture/importOrigin";
import LocationPicker from "./LocationPicker";

/** "2026-08-05T18:42:11" -> "2026-08-05T18:42", which is what the input wants. */
function toInputValue(local: string | null): string {
  return local?.slice(0, 16) ?? "";
}

function nowInputValue(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(
    d.getHours(),
  )}:${pad(d.getMinutes())}`;
}

export default function ImportOriginPrompt({
  md,
  mediaKind = "photo",
  onConfirm,
  onCancel,
}: {
  md: PhotoMetadata;
  mediaKind?: "photo" | "video";
  onConfirm: (origin: ImportOrigin) => void;
  onCancel: () => void;
}) {
  const [place, setPlace] = useState<{ lat: number; lng: number } | null>(() =>
    md.has_location && md.lat != null && md.lng != null
      ? { lat: md.lat, lng: md.lng }
      : null,
  );
  const [placeFromFile, setPlaceFromFile] = useState(md.has_location);
  const [when, setWhen] = useState(toInputValue(md.captured_at_local));
  const [picking, setPicking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function confirm() {
    // Preserve seconds and the file's offset when its displayed time is unchanged.
    const fromFile = when === toInputValue(md.captured_at_local) && md.has_date;
    const capturedAt = resolveCapturedAt(
      fromFile ? md.captured_at_local : when,
      fromFile ? md.utc_offset_minutes : null,
    );
    if (!capturedAt) {
      setError("Please give a rough date and time.");
      return;
    }
    if (new Date(capturedAt).getTime() > Date.now()) {
      setError("Please choose a date and time that isn't in the future.");
      return;
    }
    onConfirm(
      placeFromFile && place
        ? originFromExif(capturedAt, place.lat, place.lng)
        : originFromPerson(capturedAt, place),
    );
  }

  if (picking) {
    return (
      <LocationPicker
        initial={place}
        onPick={(picked) => {
          setPlace(picked);
          // Even an explicit current fix is the person's assertion about a past event.
          setPlaceFromFile(false);
          setPicking(false);
        }}
        onClose={() => setPicking(false)}
      />
    );
  }

  return (
    <div className="viewer-overlay" onClick={onCancel}>
      <div className="import-prompt" onClick={(e) => e.stopPropagation()}>
        <span className="spot-label">ABOUT THIS {mediaKind === "video" ? "CLIP" : "PHOTO"}</span>
        <p className="hint">
          {mediaKind === "video"
            ? "Tell us when and where this clip was recorded — not where you are now."
            : md.has_date || md.has_location
              ? "This photo didn't say everything about itself — fill in the rest."
              : "This photo carries no date or place, so we need a rough idea."}
        </p>

        <div className="field-group">
          <label htmlFor="import-when">roughly when was it taken?</label>
          <input
            id="import-when"
            type="datetime-local"
            value={when}
            max={nowInputValue()}
            onChange={(e) => { setWhen(e.target.value); setError(null); }}
          />
          <p className="hint">Times you enter use this device's time zone.</p>
        </div>

        <div className="field-group">
          <label>roughly where?</label>
          {place && (
            <p className="hint">
              {place.lat.toFixed(4)}, {place.lng.toFixed(4)}
              {placeFromFile ? " (from the photo)" : ""}
            </p>
          )}
          <button type="button" className="link-btn" onClick={() => setPicking(true)}>
            {place ? "change place" : "set where it was taken"}
          </button>
          {!place && <p className="hint">Without a place this sighting won't appear on the map.</p>}
        </div>

        {error && <p className="hint import-error" role="alert">{error}</p>}

        <div className="actions-row">
          <button type="button" className="btn btn-secondary" onClick={onCancel}>
            Cancel
          </button>
          <button type="button" className="btn btn-primary" onClick={confirm}>
            {place ? "Add sighting" : "Add without a place"}
          </button>
        </div>
      </div>
    </div>
  );
}
