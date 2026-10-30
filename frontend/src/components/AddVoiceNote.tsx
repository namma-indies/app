import { useState } from "react";
import { addVoiceNote, HttpError, UnauthorizedError } from "../api";
import { newClientToken } from "../clientToken";
import VoiceNote, { voiceNotesEnabled } from "./VoiceNote";

/** The server's say on later notes (/me): on at all, and how many each takes. */
export interface LaterNotesConfig {
  enabled: boolean;
  max: number;
}

/** Whether to offer "Add voice note" at all: the build has voice notes, and
 * the server has a worker transcribing them. */
export function laterNotesOffered(config: LaterNotesConfig | null): config is LaterNotesConfig {
  return !!config?.enabled && config.max > 0 && voiceNotesEnabled();
}

const FAILED: Record<number, string> = {
  409: "This already has as many added notes as it can take.",
  413: "That note is too long to save.",
  429: "You’ve added a lot of notes in the last hour. Try again later.",
  503: "Notes can’t be added right now. Try again later.",
};

/**
 * Add a voice note to a sighting already uploaded (#156): the capture
 * screen's recorder (record, play back, record again or delete), then "Save
 * note". It is transcribed and read for details like a note recorded with the
 * photos. A failed save keeps the recording so it can be tried again.
 */
export default function AddVoiceNote({ target, added, max, onAdded, onUnauthorized }: {
  target: { sightingId: string } | { captureId: string };
  /** How many notes have been added to it already. */
  added: number;
  max: number;
  onAdded: () => void;
  onUnauthorized: () => void;
}) {
  const [note, setNote] = useState<Blob | null>(null);
  // One per recording, kept across retries of it (a lost answer must not save it twice).
  const [token, setToken] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (added >= max) {
    return <p className="hint add-voice-note">{max === 1 ? "A note has" : `${max} notes have`} been added here, as many as it can take.</p>;
  }

  async function save() {
    if (!note || !token) return;
    setSaving(true);
    setError(null);
    try {
      await addVoiceNote(target, note, token);
      setNote(null);
      setToken(null);
      onAdded();
    } catch (err) {
      if (err instanceof UnauthorizedError) onUnauthorized();
      setError((err instanceof HttpError && FAILED[err.status]) || "Couldn’t save your note. Try again.");
    } finally {
      setSaving(false);
    }
  }

  return <div className="add-voice-note">
    <VoiceNote value={note} onChange={(next) => { setNote(next); setToken(next ? newClientToken() : null); setError(null); }} />
    {note && <button type="button" className="btn" disabled={saving} onClick={save}>{saving ? "Saving…" : "Save note"}</button>}
    {error && <p className="hint place-warning" role="alert">{error}</p>}
  </div>;
}
