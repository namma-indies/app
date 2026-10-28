import { useEffect, useRef, useState } from "react";
import { haptic } from "../haptics";

/** Longest note we keep. Recording stops itself here (#96). */
export const MAX_NOTE_SECONDS = 60;

/**
 * Formats the browser's MediaRecorder can write, best first. iOS WebKit writes
 * AAC in MP4; Android's WebView writes Opus in WebM. Both are stored as sent.
 */
const PREFERRED_TYPES = ["audio/mp4", "audio/webm;codecs=opus", "audio/webm"];

function pickType(): string | undefined {
  if (typeof MediaRecorder === "undefined" || !MediaRecorder.isTypeSupported) return undefined;
  return PREFERRED_TYPES.find((t) => MediaRecorder.isTypeSupported(t));
}

/** Off until launch: the recorder ships dark behind a build flag. The server
 * accepts a note either way, so a queued one from a flagged build still lands. */
export function voiceNotesEnabled(): boolean {
  return import.meta.env.VITE_VOICE_NOTES_ENABLED === "true";
}

export function canRecordVoice(): boolean {
  return (
    typeof MediaRecorder !== "undefined" &&
    typeof navigator !== "undefined" &&
    !!navigator.mediaDevices?.getUserMedia
  );
}

/**
 * Tap to start, tap to stop, then play, record again or delete before saving.
 *
 * The recorder runs in the WebView rather than through a native plugin: both
 * shells already route getUserMedia to the OS microphone prompt (Capacitor's
 * BridgeWebChromeClient on Android, WKUIDelegate on iOS), and that prompt only
 * appears when someone taps record -- never at launch.
 */
export default function VoiceNote({
  value,
  onChange,
}: {
  value: Blob | null;
  onChange: (note: Blob | null) => void;
}) {
  const [recording, setRecording] = useState(false);
  const [seconds, setSeconds] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [url, setUrl] = useState<string | null>(null);
  const recorder = useRef<MediaRecorder | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (!value) {
      setUrl(null);
      return;
    }
    const next = URL.createObjectURL(value);
    setUrl(next);
    return () => URL.revokeObjectURL(next);
  }, [value]);

  function release() {
    if (timer.current) clearInterval(timer.current);
    timer.current = null;
    stream.current?.getTracks().forEach((t) => t.stop());
    stream.current = null;
  }

  // Leaving the screen mid-note must free the microphone, or the OS keeps
  // showing it in use.
  useEffect(() => () => {
    if (recorder.current?.state === "recording") recorder.current.stop();
    release();
  }, []);

  function stop() {
    if (recorder.current?.state === "recording") recorder.current.stop();
  }

  async function start() {
    setError(null);
    let media: MediaStream;
    try {
      media = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      setError("Couldn't use the microphone. Allow it in Settings to record a note.");
      return;
    }
    stream.current = media;
    const type = pickType();
    const rec = new MediaRecorder(media, type ? { mimeType: type } : undefined);
    const chunks: Blob[] = [];
    rec.ondataavailable = (e) => {
      if (e.data.size > 0) chunks.push(e.data);
    };
    rec.onstop = () => {
      release();
      setRecording(false);
      haptic("recordStop");
      const note = new Blob(chunks, { type: (rec.mimeType || type || "audio/mp4").split(";")[0] });
      onChange(note.size > 0 ? note : null);
    };
    recorder.current = rec;
    rec.start();
    haptic("recordStart");
    setSeconds(0);
    setRecording(true);
    const began = Date.now();
    timer.current = setInterval(() => {
      const elapsed = Math.floor((Date.now() - began) / 1000);
      setSeconds(elapsed);
      if (elapsed >= MAX_NOTE_SECONDS) stop();
    }, 250);
  }

  if (!canRecordVoice()) return null;

  if (recording) {
    return (
      <div className="voice-note voice-note-recording">
        <button type="button" className="voice-btn" onClick={stop} aria-label="Stop recording">
          ■ stop
        </button>
        <span className="voice-time">
          0:{String(seconds).padStart(2, "0")} / 1:00
        </span>
      </div>
    );
  }

  if (value && url) {
    return (
      <div className="voice-note">
        <audio src={url} controls preload="metadata" />
        <button type="button" className="link-btn" onClick={start}>
          record again
        </button>
        <button type="button" className="link-btn" onClick={() => onChange(null)}>
          delete
        </button>
      </div>
    );
  }

  return (
    <div className="voice-note">
      <button type="button" className="voice-btn" onClick={start}>
        🎙 add a voice note
      </button>
      {error && <p className="hint place-warning">{error}</p>}
    </div>
  );
}
