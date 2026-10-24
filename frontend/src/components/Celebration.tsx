import { useEffect, useMemo, useRef, useState } from "react";

import { UPLOAD_COMPLETE_EVENT } from "../processing";
import { recordAcknowledged, type Streak } from "../streak";
import CatSprite, { type CatCoat } from "./CatSprite";
import DogSprite, { type Coat } from "./DogSprite";
import StreakCard from "./StreakCard";

/**
 * The moment of arrival.
 *
 * Fires on one signal only: `UPLOAD_COMPLETE_EVENT`, which the offline queue
 * dispatches after the server has answered and the local copy has been deleted.
 * That is the only point at which "logged" is true -- before it, the capture is
 * merely saved on the device, and saying otherwise to someone standing on a
 * hillside with one bar of signal would be a lie.
 *
 * It deliberately claims nothing about the animal. Detection and matching run
 * later, elsewhere, and may find nothing at all.
 *
 * Several acknowledgements inside one celebration are one party that says how
 * many (#81), and the party grows with the count. A run of sightings close
 * together in capture time (see streak.ts) opens the streak card on top.
 */

export const CELEBRATION_MS = 2400;

const PIECES = 16;
/** Confetti grows with the count, up to here. */
const MAX_PIECES = 48;
const DOG_COATS: Coat[] = ["tan", "black", "piebald", "brindle", "ghost"];
const CAT_COATS: CatCoat[] = ["ginger", "sooty", "tabby", "calico", "smoke"];
const CONFETTI = ["var(--accent)", "var(--accent-2)", "var(--leaf)", "var(--indigo)", "var(--frame)"];

function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    // Older WebViews throw on an unsupported query rather than returning false.
    return false;
  }
}

type Piece = { left: number; delay: number; spin: number; fall: number; colour: string };
type Runner = { kind: "dog" | "cat"; coat: number; delay: number; bottom: number; dur: number };

function scatter(run: number, count: number): { pieces: Piece[]; runners: Runner[] } {
  // Seeded off the run counter so a re-render mid-celebration doesn't reshuffle
  // everything into a new arrangement halfway through.
  let seed = run * 9301 + 49297;
  const rnd = () => ((seed = (seed * 9301 + 49297) % 233280) / 233280);

  const pieces: Piece[] = Array.from({ length: Math.min(MAX_PIECES, PIECES * Math.max(1, count)) }, () => ({
    left: Math.round(rnd() * 96) + 2,
    delay: Math.round(rnd() * 420),
    spin: Math.round(rnd() * 360),
    fall: 0.9 + rnd() * 0.7,
    colour: CONFETTI[Math.floor(rnd() * CONFETTI.length)],
  }));

  const runners: Runner[] = [
    { kind: "dog", coat: Math.floor(rnd() * DOG_COATS.length), delay: 0, bottom: 14, dur: 1.5 },
    { kind: "cat", coat: Math.floor(rnd() * CAT_COATS.length), delay: 260, bottom: 30, dur: 1.75 },
    { kind: "dog", coat: Math.floor(rnd() * DOG_COATS.length), delay: 520, bottom: 6, dur: 1.4 },
  ];
  // A burst gets a fourth runner: the visual is part of the readout.
  if (count >= 3) {
    runners.push({ kind: "cat", coat: Math.floor(rnd() * CAT_COATS.length), delay: 780, bottom: 22, dur: 1.6 });
  }

  return { pieces, runners };
}

export default function Celebration() {
  const [run, setRun] = useState(0);
  const [count, setCount] = useState(0);
  const [streak, setStreak] = useState<Streak | null>(null);
  const [quiet, setQuiet] = useState(false);
  const running = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout>>();

  useEffect(() => {
    function logged(event: Event) {
      const capturedAt = (event as CustomEvent<{ captured_at?: string } | undefined>).detail?.captured_at;
      const completed = recordAcknowledged(capturedAt);
      // One card at a time; a second streak can't complete while one is open.
      if (completed) setStreak((open) => open ?? completed);
      // A queue catching up flushes its whole backlog in one pass, so several
      // acknowledgements land within milliseconds. One party, not six stacked
      // full-screen layers -- but it now says how many.
      if (running.current) {
        setCount((n) => n + 1);
        return;
      }
      running.current = true;
      setQuiet(prefersReducedMotion());
      setCount(1);
      setRun((n) => n + 1);
      timer.current = setTimeout(() => {
        running.current = false;
        setRun(0);
        setCount(0);
      }, CELEBRATION_MS);
    }

    window.addEventListener(UPLOAD_COMPLETE_EVENT, logged);
    return () => {
      window.removeEventListener(UPLOAD_COMPLETE_EVENT, logged);
      clearTimeout(timer.current);
      running.current = false;
    };
  }, []);

  const { pieces, runners } = useMemo(() => scatter(run, count), [run, count]);

  const card = streak && <StreakCard streak={streak} onClose={() => setStreak(null)} />;
  if (run === 0) return card || null;

  return (
    <>
    {card}
    <div className="celebration">
      {!quiet && (
        <div className="celebration-scene" aria-hidden="true">
          {pieces.map((p, i) => (
            <span
              key={i}
              className="confetti-piece"
              style={{
                left: `${p.left}%`,
                background: p.colour,
                animationDelay: `${p.delay}ms`,
                animationDuration: `${p.fall + 0.9}s`,
                ["--spin" as string]: `${p.spin}deg`,
              }}
            />
          ))}
          {runners.map((r, i) => (
            <span
              key={i}
              className="celebration-runner"
              style={{
                bottom: `${r.bottom}%`,
                animationDelay: `${r.delay}ms`,
                animationDuration: `${r.dur}s`,
              }}
            >
              {r.kind === "dog" ? (
                <DogSprite coat={DOG_COATS[r.coat]} scale={4} />
              ) : (
                <CatSprite coat={CAT_COATS[r.coat]} scale={4} />
              )}
            </span>
          ))}
        </div>
      )}
      <p className={quiet ? "celebration-badge is-quiet" : "celebration-badge"} role="status">
        <strong>{count > 1 ? `${count} logged!` : "Logged!"}</strong>
        <span>Saved to the server</span>
      </p>
    </div>
    </>
  );
}
