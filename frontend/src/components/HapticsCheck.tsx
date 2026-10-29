import { useEffect, useRef, useState } from "react";
import { Capacitor } from "@capacitor/core";
import { ALL_MOMENTS, haptic, hapticChannel, onHapticOutcome, playScore, switchSupported, type Moment } from "../haptics";
import { sampleSuggestionScore } from "../suggestionFlow";

/** The check is shown on the web by `?haptics=check`, and in the apps by a long-press on the brand. */
export const HAPTICS_CHECK_PARAM = "haptics";
export const wantsHapticsCheck = (search: string) => new URLSearchParams(search).get(HAPTICS_CHECK_PARAM) === "check";

const platformName = () => {
  try {
    return `${Capacitor.getPlatform()}${Capacitor.isNativePlatform() ? " (app)" : " (browser)"}`;
  } catch {
    return "unknown";
  }
};
const yesNo = (value: boolean) => (value ? "yes" : "no");

/** The app's version and build, from @capacitor/app. Only the native apps have them. */
async function appBuild(): Promise<string> {
  if (!Capacitor.isNativePlatform()) return "web (no app build)";
  try {
    const { App } = await import("@capacitor/app");
    const info = await App.getInfo();
    return `${info.version} (build ${info.build})`;
  } catch (error) {
    return `unknown (${error instanceof Error ? error.message : String(error)})`;
  }
}

/** Why a buzz might not happen, read fresh each time it is shown. */
function readStatus() {
  const nav = typeof navigator === "undefined" ? undefined : navigator;
  let plugin = false;
  try { plugin = Capacitor.isNativePlatform() && Capacitor.isPluginAvailable("Haptics"); } catch { /* shown as no */ }
  return [
    ["Platform", platformName()],
    ["Native haptics plugin", yesNo(plugin)],
    ["navigator.vibrate", yesNo(typeof nav?.vibrate === "function")],
    ["User activation (page has been tapped)", nav?.userActivation ? yesNo(nav.userActivation.hasBeenActive) : "not reported"],
    ["User activation (tap right now)", nav?.userActivation ? yesNo(nav.userActivation.isActive) : "not reported"],
    ["iOS switch trick", yesNo(switchSupported())],
    ["Channel a buzz would use", hapticChannel() ?? "none"],
  ] as const;
}

/**
 * Hidden check that haptics work on this phone. Shows what the platform offers and plays each
 * named moment on demand. Changes no data: nothing here reads or writes the app's records.
 */
export default function HapticsCheck({ onClose }: { onClose: () => void }) {
  const [build, setBuild] = useState("…");
  const [log, setLog] = useState<string[]>([]);
  const [, refresh] = useState(0);
  const stopPattern = useRef<() => void>(() => {});

  useEffect(() => {
    let live = true;
    appBuild().then((value) => { if (live) setBuild(value); });
    const stop = onHapticOutcome((outcome) => {
      setLog((lines) => [`${new Date().toLocaleTimeString()}  ${outcome}`, ...lines].slice(0, 30));
      refresh((n) => n + 1);
    });
    return () => { live = false; stop(); stopPattern.current(); };
  }, []);

  const play = (moment: Moment) => { haptic(moment); refresh((n) => n + 1); };
  // A buzz some time after the tap: shows whether this phone still counts the tap by then (it
  // decides if Save, which buzzes after the server answers, can tick in an iPhone browser).
  const playLater = (moment: Moment) => { window.setTimeout(() => play(moment), 1500); };
  // What a voice note's suggestions feel like (#144): the same pulses the animation plays, without it.
  const playPattern = () => { stopPattern.current(); stopPattern.current = playScore(sampleSuggestionScore(), "suggestion"); };

  return (
    <div className="haptics-check" role="dialog" aria-label="Haptics check">
      <h1>Haptics check</h1>
      <p className="hint">Changes no data. Turn the phone's ringer/vibration on and tap each moment.</p>
      <dl>
        {[["App version", build], ...readStatus()].map(([name, value]) => (
          <div key={name}><dt>{name}</dt><dd>{value}</dd></div>
        ))}
      </dl>
      <h2>Moments (started by this tap)</h2>
      <div className="haptics-buttons">
        {ALL_MOMENTS.map((moment) => (
          <button key={moment} type="button" onClick={() => play(moment)}>{moment}</button>
        ))}
      </div>
      <h2>Same moments, 1.5 s after the tap</h2>
      <div className="haptics-buttons">
        {ALL_MOMENTS.map((moment) => (
          <button key={moment} type="button" onClick={() => playLater(moment)}>{moment} later</button>
        ))}
      </div>
      <h2>The suggestion pattern</h2>
      <p className="hint">
        What a voice note's suggestions feel like, about 4.5 s: soft ticks as the shimmer scans, a light tap as each of
        three phrases lights up, ticks getting firmer and closer while its words fly, a heavy tap as each lands, a success last.
      </p>
      <div className="haptics-buttons">
        <button type="button" onClick={playPattern}>Play the suggestion pattern</button>
      </div>
      <h2>What happened</h2>
      <pre className="haptics-log" aria-live="polite">{log.length ? log.join("\n") : "Nothing yet."}</pre>
      <button type="button" className="haptics-close" onClick={onClose}>Close</button>
    </div>
  );
}
