import { useState } from "react";
import type { PhotoMetadata } from "../api";
import {
  originFromExif,
  originFromPerson,
  resolveCapturedAt,
  type ImportOrigin,
} from "../capture/importOrigin";

/**
 * Asks for what an imported photo's file didn't say.
 *
 * Reached whenever a camera-roll photo is missing its date or its coordinates.
 * That is the common path, not the edge: WhatsApp forwards, screenshots and
 * several gallery apps strip EXIF entirely, and an offline preflight looks
 * identical. The alternative to asking is defaulting to here-and-now, which
 * silently inserts a phantom sighting into the 1km spatial prior that
 * re-identification matches against.
 *
 * Deliberately plain: this is a placeholder while the real interaction gets
 * designed (see the follow-up issue). It only has to be correct, not lovely --
 * so it asks for a rough time and offers one location, rather than pretending
 * to a map picker it doesn't have.
 */

/** "2026-08-05T18:42:11" -> "2026-08-05T18:42", which is what the input wants. */
function toInputValue(local: string | null): string {
  if (!local) return "";
  return local.slice(0, 16);
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
  getPosition,
  onConfirm,
  onCancel,
}: {
  md: PhotoMetadata;
  getPosition: () => Promise<{ lat: number; lng: number } | null>;
  onConfirm: (origin: ImportOrigin) => void;
  onCancel: () => void;
}) {
  const knownLocation =
    md.has_location && md.lat != null && md.lng != null
      ? { lat: md.lat, lng: md.lng }
      : null;

  const [when, setWhen] = useState(toInputValue(md.captured_at_local));
  const [place, setPlace] = useState<{ lat: number; lng: number } | null>(knownLocation);
  const [locating, setLocating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function useCurrentLocation() {
    setLocating(true);
    setError(null);
    const pos = await getPosition();
    setLocating(false);
    if (!pos) {
      setError("Couldn't get your location. You can still save without it.");
      return;
    }
    setPlace(pos);
  }

  function confirm(withPlace: { lat: number; lng: number } | null) {
    // The offset is only meaningful for a value that came from the file. A time
    // typed into the input is in the device's zone, which is what a null offset
    // means to resolveCapturedAt.
    const fromFile = when === toInputValue(md.captured_at_local) && md.has_date;
    const capturedAt = resolveCapturedAt(
      when,
      fromFile ? md.utc_offset_minutes : null,
    );
    if (!capturedAt) {
      setError("Please give a rough date and time.");
      return;
    }
    onConfirm(
      // A coordinate the file itself carried stays `exif`; one the person
      // supplied is a `pin`, because they're asserting where it happened.
      knownLocation && withPlace === knownLocation
        ? originFromExif(capturedAt, knownLocation.lat, knownLocation.lng)
        : originFromPerson(capturedAt, withPlace),
    );
  }

  return (
    <div className="viewer-overlay" onClick={onCancel}>
      <div className="import-prompt" onClick={(e) => e.stopPropagation()}>
        <span className="spot-label">ABOUT THIS PHOTO</span>
        <p className="hint">
          {md.has_date || md.has_location
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
            onChange={(e) => setWhen(e.target.value)}
          />
        </div>

        <div className="field-group">
          <label>roughly where?</label>
          {place ? (
            <p className="hint">
              {place.lat.toFixed(4)}, {place.lng.toFixed(4)}
              {knownLocation && place === knownLocation ? " (from the photo)" : ""}
            </p>
          ) : (
            <button
              type="button"
              className="link-btn"
              disabled={locating}
              onClick={useCurrentLocation}
            >
              {locating ? "finding you…" : "use my current location"}
            </button>
          )}
        </div>

        {error && <p className="hint import-error">{error}</p>}

        <div className="actions-row">
          <button type="button" className="btn btn-secondary" onClick={onCancel}>
            Cancel
          </button>
          {/* Saving without a place is allowed on purpose. A sighting with no
              coordinate still counts -- it just draws no pin, exactly like an
              offline capture with geo_source=none. Refusing it would throw away
              a real photo of a real dog over a field nobody remembers. */}
          <button type="button" className="btn btn-primary" onClick={() => confirm(place)}>
            {place ? "Add sighting" : "Add without a place"}
          </button>
        </div>
      </div>
    </div>
  );
}
