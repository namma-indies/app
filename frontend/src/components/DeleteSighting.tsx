import { useState } from "react";
import { deleteCapture, deleteSighting, UnauthorizedError } from "../api";
import { speciesNoun } from "../species";

export const DELETE_PHOTO_WARNING =
  "Delete this photo? It goes for everyone, with every animal in it, its voice notes and their words. This can't be undone.";

export function removeAnimalWarning(noun: string): string {
  return `Remove this ${noun}? Its picture and details go, and it leaves the shared map. The photo and the other animals in it stay. This can't be undone.`;
}

/**
 * The owner's way to take back something they logged (#54). "Delete photo"
 * is the whole upload; on a photo with several animals, "Remove this dog"
 * takes just this one. Asks first: nothing comes back.
 */
export default function DeleteSighting({ sightingId, captureId, species, animalsInPhoto, onDeleted, onUnauthorized }: {
  sightingId: string;
  captureId?: string | null;
  species?: string | null;
  /** How many of your sightings came from the same upload. */
  animalsInPhoto: number;
  /** "photo": the whole upload went; "animal": just this one. */
  onDeleted: (what: "photo" | "animal") => void;
  onUnauthorized: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const noun = speciesNoun(species);

  async function run(what: "photo" | "animal", warning: string, action: () => Promise<void>) {
    if (!confirm(warning)) return;
    setBusy(true);
    setFailed(false);
    try {
      await action();
      onDeleted(what);
    } catch (err) {
      if (err instanceof UnauthorizedError) onUnauthorized();
      else setFailed(true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="viewer-delete">
      {animalsInPhoto > 1 && (
        <button type="button" className="link-btn" disabled={busy}
          onClick={() => run("animal", removeAnimalWarning(noun), () => deleteSighting(sightingId))}>
          Remove this {noun}
        </button>
      )}
      <button type="button" className="link-btn" disabled={busy}
        onClick={() => run("photo", DELETE_PHOTO_WARNING, () => captureId ? deleteCapture(captureId) : deleteSighting(sightingId))}>
        Delete photo
      </button>
      {failed && <p className="hint place-warning" role="alert">Couldn’t delete that just now. Try again.</p>}
    </div>
  );
}
