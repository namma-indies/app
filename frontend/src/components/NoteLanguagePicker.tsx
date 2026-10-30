import { useEffect, useState } from "react";
import { getMe, setNoteLanguage, UnauthorizedError, type NoteLanguage } from "../api";

export const NOTE_LANGUAGES: { code: NoteLanguage; label: string; short: string }[] = [
  { code: "en", label: "English", short: "EN" },
  { code: "kn", label: "Kannada", short: "KN" },
  { code: "hi", label: "Hindi", short: "HI" },
  { code: "ta", label: "Tamil", short: "TA" },
  { code: "te", label: "Telugu", short: "TE" },
  { code: "ml", label: "Malayalam", short: "ML" },
  { code: "auto", label: "Mixed / let the app decide", short: "MIXED" },
];
const byCode = Object.fromEntries(NOTE_LANGUAGES.map((l) => [l.code, l]));

// An answer not yet saved (the phone offline, say), with whose it is: kept
// until the server has that person's answer, and only ever used once /me has
// said that same person is signed in. Never offline: nothing here can tell
// who that is, and an answer must not reach anyone else's notes.
const UNSAVED = "indiedex.noteLanguage.unsaved";

/** Forget any kept answer: on sign-out, so the next person starts clean. */
export function forgetNoteLanguage() {
  write(UNSAVED, null);
}

function read(key: string): string | null {
  try { return localStorage.getItem(key); } catch { return null; }
}
function write(key: string, value: string | null) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch { /* private mode: nothing kept; the answer still goes with each note */ }
}

function unsaved(observer: string | null): NoteLanguage | null {
  if (!observer) return null;
  try {
    const kept = JSON.parse(read(UNSAVED) ?? "null") as { observer?: string; language?: string } | null;
    return kept?.observer === observer && NOTE_LANGUAGES.some((l) => l.code === kept.language)
      ? (kept.language as NoteLanguage) : null;
  } catch {
    return null;
  }
}

async function save(observer: string | null, language: NoteLanguage, onUnauthorized?: () => void) {
  try {
    await setNoteLanguage(language);
    if (unsaved(observer) === language) write(UNSAVED, null);
  } catch (err) {
    if (err instanceof UnauthorizedError) onUnauthorized?.();
  }
}

/** The observer's usual note language: undefined while loading (or if /me
 * failed: then nothing is asked or sent), null until they answer (#167). An
 * answer whose save failed is kept on the device for that observer only,
 * shown again once /me says they are back, saved then, and sent with each
 * upload meanwhile (usual_language), so the server still gets it. */
export function useNoteLanguage(onUnauthorized?: () => void) {
  const [usual, setUsual] = useState<NoteLanguage | null | undefined>(undefined);
  const [observer, setObserver] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    getMe().then((me) => {
      if (!live) return;
      setObserver(me.id);
      const pending = unsaved(me.id);
      if (me.note_language == null && pending) {
        setUsual(pending);
        void save(me.id, pending, onUnauthorized);
      } else {
        // Theirs is on the server already, or what is kept is someone else's: let it go.
        if (read(UNSAVED)) write(UNSAVED, null);
        setUsual(me.note_language ?? null);
      }
    }).catch((err) => { if (err instanceof UnauthorizedError) onUnauthorized?.(); });
    return () => { live = false; };
  }, [onUnauthorized]);
  async function keep(language: NoteLanguage) {
    setUsual(language);                       // answered: not asked again, even if saving fails now
    // Only asked once /me has said who this is (usual is null only then).
    if (observer) write(UNSAVED, JSON.stringify({ observer, language }));
    await save(observer, language, onUnauthorized);
  }
  return { usual, keep };
}

/**
 * Which language a note is spoken in (#167), next to the recorder.
 *
 * Until someone answers, one line asks what they usually speak; recording
 * without answering leaves that note to the app (mixed) and asks again next
 * time. Once answered, a small chip shows the language ("EN ▾"); changing it
 * applies to this note alone, and makes the note be transcribed in exactly
 * that language.
 */
export default function NoteLanguagePicker({ usual, onKeep, chosen, onChoose }: {
  usual: NoteLanguage | null | undefined;
  onKeep: (language: NoteLanguage) => void;
  /** The language picked for this note, or null to go with the usual one. */
  chosen: NoteLanguage | null;
  onChoose: (language: NoteLanguage | null) => void;
}) {
  const [open, setOpen] = useState(false);
  if (usual === null) {
    return <div className="note-language note-language-ask">
      <p className="spot-label" id="note-language-ask">What language do you usually speak your notes in?</p>
      <div className="chip-row" role="group" aria-labelledby="note-language-ask">
        {NOTE_LANGUAGES.map((l) => <button key={l.code} type="button" className="chip" onClick={() => onKeep(l.code)}>{l.label}</button>)}
      </div>
    </div>;
  }
  const current = byCode[chosen ?? usual ?? "auto"];
  return <div className="note-language">
    <button type="button" className="chip note-language-chip" aria-expanded={open}
      aria-label={`Language for this note: ${current.label}. Change`} onClick={() => setOpen((v) => !v)}>
      {current.short} ▾
    </button>
    {open && <div className="chip-row" role="group" aria-label="Language for this note">
      {NOTE_LANGUAGES.map((l) => <button key={l.code} type="button" aria-pressed={l.code === current.code}
        className={l.code === current.code ? "chip active" : "chip"}
        onClick={() => { onChoose(l.code === usual ? null : l.code); setOpen(false); }}>{l.label}</button>)}
    </div>}
  </div>;
}
