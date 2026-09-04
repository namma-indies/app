import { useEffect, useRef, useState } from "react";
import {
  isAvailable,
  onAnimals,
  start,
  stop,
  type AnimalsEvent,
} from "../native/dogSegmenter";
import type { PluginListenerHandle } from "@capacitor/core";

/**
 * The segmenting viewfinder.
 *
 * Almost everything on this screen is drawn by the native layer underneath a
 * transparent WebView: the camera, the masks, the boxes, the frame rate. This
 * component is deliberately near-empty — it owns the controls and the honest
 * copy, and nothing else. Rendering the masks here would mean shipping ~6,400
 * floats per animal per frame across the bridge, which is not a budget that
 * exists at 30 fps.
 *
 * It does not capture anything. This is a viewfinder that can see, not a second
 * capture path: the existing SPOT flow is untouched, and adding a shutter here
 * would fork the one part of the app that must not have two implementations.
 * What it is for is the moment before the shutter — knowing there are two dogs
 * in front of you, and that the app has noticed both.
 */
export default function LiveView({ onClose }: { onClose: () => void }) {
  const [state, setState] = useState<AnimalsEvent | null>(null);
  const [error, setError] = useState<string | null>(null);
  const listener = useRef<PluginListenerHandle | null>(null);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        listener.current = await onAnimals((e) => {
          if (!cancelled) setState(e);
        });
        await start({ size: 320 });
      } catch (e) {
        if (!cancelled) {
          const message = e instanceof Error ? e.message : String(e);
          // Named rather than smoothed over. The two realistic failures are a
          // refused camera permission and a missing model asset, and they need
          // different actions from whoever is holding the phone.
          setError(
            /permission/i.test(message)
              ? "Camera access is needed for the live view."
              : `Couldn't start the live view. ${message}`,
          );
        }
      }
    })();

    return () => {
      cancelled = true;
      // Order matters: drop the listener before stopping, or a final event can
      // arrive after unmount and set state on a dead component.
      listener.current?.remove();
      listener.current = null;
      void stop();
    };
  }, []);

  const animals = state?.animals ?? [];
  const dogs = animals.filter((a) => a.kind === "dog").length;

  return (
    <div className="live-view">
      <div className="live-top">
        <button className="btn btn-secondary" onClick={onClose}>
          CLOSE
        </button>
        {state && !state.gpu && (
          // Said out loud. minSdk is 24, so some devices land on the CPU, and
          // an app quietly running at a few frames a second looks broken with
          // no way for anyone to tell why.
          <span className="live-warn">running on CPU — will be slow</span>
        )}
      </div>

      {error ? (
        <div className="live-error" role="alert">
          <p>{error}</p>
          <button className="btn btn-primary" onClick={onClose}>
            GO BACK
          </button>
        </div>
      ) : (
        <div className="live-bottom">
          <p className="live-count">
            {animals.length === 0
              ? "looking…"
              : `${animals.length} animal${animals.length === 1 ? "" : "s"}` +
                (dogs !== animals.length ? ` (${dogs} dog${dogs === 1 ? "" : "s"})` : "")}
          </p>
          {animals.length > 1 && (
            /* The entire reason this screen exists. The server keeps only the
               largest animal in a frame, so without this a second dog is
               logged as if it were never there. */
            <p className="hint">
              More than one animal here — take a photo of each one separately so
              both get logged.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

/** Whether to offer the live view at all. An affordance that always fails is
 * worse than an absent one. */
export const liveViewAvailable = isAvailable;
