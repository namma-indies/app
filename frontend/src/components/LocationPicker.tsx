import { useEffect, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { basemapStyle } from "./DogMap";
import { howToReEnable, locate, permissionState } from "../capture/geolocate";

/**
 * Saying where a sighting happened, when the phone cannot say it for you.
 *
 * WHY THIS EXISTS
 * ---------------
 * A live capture had exactly two possible outcomes: the device's own fix, or
 * nothing. `geo_source: "pin"` was already in the type and already accepted by
 * the server, but the only UI that could produce one was the camera-roll import
 * path. So a tester in Kodaikanal with poor GPS, who knew perfectly well where
 * they were and even had the coordinates written down, had no way to say so.
 *
 * Their sighting then saved with no coordinate at all, which is worse than it
 * sounds: `/map` filters on `geog IS NOT NULL`, so it never appeared on any map
 * and looked like it had failed to send.
 *
 * THREE WAYS IN, BECAUSE THEY FAIL DIFFERENTLY
 * -------------------------------------------
 * - The device fix, retried properly. Right almost always, and now it accepts a
 *   recent cached fix and falls back to a coarse one instead of giving up.
 * - A pin on a map, for "I know where I am, the phone doesn't". Pan-under-a-
 *   fixed-crosshair rather than a draggable marker: on a phone your thumb
 *   covers a marker you are dragging, and the crosshair stays visible.
 * - Typed coordinates, because the person who asked for this had them.
 *
 * PERMISSION IS A ONE-WAY DOOR IN A BROWSER
 * -----------------------------------------
 * Once refused, a page cannot re-prompt. So a refusal is met with words instead
 * — the actual menu path for this platform — and the other two routes stay
 * open. "Enable location in settings" would be useless advice on a phone.
 */

export interface PickedPlace {
  lat: number;
  lng: number;
  /** `device_gps` when the phone measured it, `pin` when a person asserted it.
   * The distinction is load-bearing: it is the difference between an accuracy
   * we measured and one we are being told. */
  source: "device_gps" | "pin";
  accuracy?: number;
}

const BANGALORE: [number, number] = [77.5946, 12.9716];

/** Below this, a pin is cheap to drop and easy to be kilometres off. A tap at
 *  zoom 11 can cover an entire town; the candidate search the server runs on
 *  `geog` wants street-level precision, not "somewhere near here". */
const MIN_PICK_ZOOM = 14;

const LAST_PLACE_KEY = "indiedex:lastPinnedPlace";

/** Where the map centres when nobody has picked anything for THIS sighting
 *  yet. Falling back to a hardcoded Bangalore pin was fine in the office; a
 *  tester opening the map cold out in Kodaikanal had to pan across a few
 *  hundred kilometres before reaching their own street. */
function readLastPinnedPlace(): { lat: number; lng: number } | null {
  try {
    if (typeof localStorage === "undefined") return null;
    const raw = localStorage.getItem(LAST_PLACE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (typeof parsed?.lat === "number" && typeof parsed?.lng === "number") {
      return { lat: parsed.lat, lng: parsed.lng };
    }
    return null;
  } catch {
    return null;
  }
}

function writeLastPinnedPlace(p: { lat: number; lng: number }): void {
  try {
    if (typeof localStorage === "undefined") return;
    localStorage.setItem(LAST_PLACE_KEY, JSON.stringify(p));
  } catch {
    // Private browsing / full quota: losing this is a convenience lost, not
    // a correctness problem, so there's nothing to surface to the person.
  }
}

type Mode = "choose" | "map" | "coords";

export default function LocationPicker({
  initial,
  onPick,
  onClose,
}: {
  initial: { lat: number; lng: number } | null;
  onPick: (place: PickedPlace | null) => void;
  onClose: () => void;
}) {
  const [mode, setMode] = useState<Mode>("choose");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [denied, setDenied] = useState(false);
  const [lat, setLat] = useState(initial ? String(initial.lat) : "");
  const [lng, setLng] = useState(initial ? String(initial.lng) : "");

  const active = useRef(true);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const centreRef = useRef<{ lat: number; lng: number }>(
    initial ?? readLastPinnedPlace() ?? { lat: BANGALORE[1], lng: BANGALORE[0] },
  );

  useEffect(() => {
    active.current = true;
    permissionState().then((s) => { if (active.current) setDenied(s === "denied"); });
    return () => { active.current = false; };
  }, []);

  useEffect(() => {
    if (mode !== "map" || !containerRef.current || mapRef.current) return;
    const knownPlace = initial ?? readLastPinnedPlace();
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: basemapStyle(),
      center: [centreRef.current.lng, centreRef.current.lat],
      zoom: knownPlace ? 16 : 11,
      attributionControl: { compact: true },
    });
    mapRef.current = map;
    // Lets a person centre the map on themselves instead of hand-panning to
    // it -- the same fix as "USE MY LOCATION" above, but available once
    // they're already in map mode rather than only from the choose screen.
    map.addControl(
      new maplibregl.GeolocateControl({
        positionOptions: { enableHighAccuracy: true },
        trackUserLocation: false,
      }),
      "top-right",
    );
    // The centre IS the pin, so it has to be read on every move rather than
    // from a marker object that does not exist. Covers GeolocateControl's own
    // pan too, since that fires "move" like any other.
    map.on("move", () => {
      const c = map.getCenter();
      centreRef.current = { lat: c.lat, lng: c.lng };
    });
    return () => {
      map.remove();
      mapRef.current = null;
    };
  }, [mode, initial]);

  async function useDevice() {
    setBusy(true);
    setError(null);
    const got = await locate();
    if (!active.current) return;
    setBusy(false);
    if (got.ok) {
      onPick({
        lat: got.lat,
        lng: got.lng,
        source: "device_gps",
        accuracy: got.accuracy ?? undefined,
      });
      return;
    }
    if (got.reason === "denied") {
      setDenied(true);
      setError("Location is turned off for IndieDex.");
    } else if (got.reason === "unsupported") {
      setError("This device can't report a location. Use the map or coordinates.");
    } else {
      // Named as not-your-fault, because the next thing they should do is use
      // one of the other two routes rather than tap this again.
      setError("Couldn't get a fix here — signal is probably poor. Try the map.");
    }
  }

  function confirmCoords() {
    const la = Number(lat.trim());
    const ln = Number(lng.trim());
    if (!lat.trim() || !lng.trim() || Number.isNaN(la) || Number.isNaN(ln)) {
      setError("Enter both, as decimal degrees. E.g. 10.2381, 77.4892");
      return;
    }
    // The server range-checks these too, and PostGIS would otherwise WRAP an
    // out-of-range latitude into a real coordinate somewhere else entirely.
    if (la < -90 || la > 90) {
      setError("Latitude must be between -90 and 90.");
      return;
    }
    if (ln < -180 || ln > 180) {
      setError("Longitude must be between -180 and 180.");
      return;
    }
    writeLastPinnedPlace({ lat: la, lng: ln });
    onPick({ lat: la, lng: ln, source: "pin" });
  }

  function useMapPick() {
    const zoom = mapRef.current?.getZoom() ?? 0;
    if (zoom < MIN_PICK_ZOOM) {
      setError("Zoom in closer first — a pin dropped this far out could be off by kilometres.");
      return;
    }
    const picked = { lat: centreRef.current.lat, lng: centreRef.current.lng };
    writeLastPinnedPlace(picked);
    onPick({ ...picked, source: "pin" });
  }

  return (
    <div className="viewer-overlay" onClick={onClose}>
      <div className="location-picker" onClick={(e) => e.stopPropagation()}>
        <span className="spot-label">WHERE WAS THIS?</span>

        {mode === "choose" && (
          <>
            <p className="hint">
              A sighting with no place doesn't appear on the map, so it's worth
              getting roughly right. Roughly is fine.
            </p>

            <button
              className="btn btn-primary"
              disabled={busy}
              onClick={useDevice}
            >
              {busy ? "FINDING YOU…" : "USE MY LOCATION"}
            </button>

            {error && (
              <p className="hint import-error" role="alert">
                {error}
              </p>
            )}
            {denied && (
              <p className="hint permission-help">
                <strong>To turn it back on:</strong> {howToReEnable()}
              </p>
            )}

            <div className="signin-or">or</div>
            <button className="btn btn-secondary" onClick={() => { setMode("map"); setError(null); }}>
              PICK ON A MAP
            </button>
            <button className="btn btn-secondary" onClick={() => { setMode("coords"); setError(null); }}>
              ENTER COORDINATES
            </button>
            <button className="link-btn" onClick={() => onPick(null)}>
              save without a place
            </button>
          </>
        )}

        {mode === "map" && (
          <>
            <p className="hint">Move the map so the cross is on the spot.</p>
            {error && (
              <p className="hint import-error" role="alert">
                {error}
              </p>
            )}
            <div className="pick-map-wrap">
              <div ref={containerRef} className="pick-map" />
              {/* Fixed crosshair over a moving map. A draggable marker would sit
                  under the thumb dragging it. */}
              <div className="pick-crosshair" aria-hidden="true">✛</div>
            </div>
            <div className="actions-row">
              <button
                className="btn btn-secondary"
                onClick={() => { setMode("choose"); setError(null); }}
              >
                BACK
              </button>
              <button className="btn btn-primary" onClick={useMapPick}>
                USE THIS SPOT
              </button>
            </div>
          </>
        )}

        {mode === "coords" && (
          <>
            <p className="hint">Decimal degrees. Kodaikanal is about 10.2381, 77.4892.</p>
            <div className="field-group">
              <label htmlFor="pick-lat">latitude</label>
              <input
                id="pick-lat"
                inputMode="decimal"
                value={lat}
                onChange={(e) => setLat(e.target.value)}
                placeholder="10.2381"
              />
            </div>
            <div className="field-group">
              <label htmlFor="pick-lng">longitude</label>
              <input
                id="pick-lng"
                inputMode="decimal"
                value={lng}
                onChange={(e) => setLng(e.target.value)}
                placeholder="77.4892"
              />
            </div>
            {error && (
              <p className="hint import-error" role="alert">
                {error}
              </p>
            )}
            <div className="actions-row">
              <button className="btn btn-secondary" onClick={() => setMode("choose")}>
                BACK
              </button>
              <button className="btn btn-primary" onClick={confirmCoords}>
                USE THESE
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
