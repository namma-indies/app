import { useEffect, useMemo, useRef, useState } from "react";

import { UPLOAD_COMPLETE_EVENT } from "../processing";
import CatSprite, { type CatCoat } from "./CatSprite";
import DogSprite, { type Coat } from "./DogSprite";

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
 */

export const CELEBRATION_MS = 2400;

const PIECES = 16;
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

function scatter(run: number): { pieces: Piece[]; runners: Runner[] } {
  // Seeded off the run counter so a re-render mid-celebration doesn't reshuffle
  // everything into a new arrangement halfway through.
  let seed = run * 9301 + 49297;
  const rnd = () => ((seed = (seed * 9301 + 49297) % 233280) / 233280);

  const pieces: Piece[] = Array.from({ length: PIECES }, () => ({
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

  return { pieces, runners };
}

export default function Celebration() {
  const [run, setRun] = useState(0);
  const [quiet, setQuiet] = useState(false);
  const running = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout>>();

  useEffect(() => {
    function logged() {
      // A queue catching up flushes its whole backlog in one pass, so several
      // acknowledgements land within milliseconds. One party, not six stacked
      // full-screen layers.
      if (running.current) return;
      running.current = true;
      setQuiet(prefersReducedMotion());
      setRun((n) => n + 1);
      timer.current = setTimeout(() => {
        running.current = false;
        setRun(0);
      }, CELEBRATION_MS);
    }

    window.addEventListener(UPLOAD_COMPLETE_EVENT, logged);
    return () => {
      window.removeEventListener(UPLOAD_COMPLETE_EVENT, logged);
      clearTimeout(timer.current);
      running.current = false;
    };
  }, []);

  const { pieces, runners } = useMemo(() => scatter(run), [run]);

  if (run === 0) return null;

  return (
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
        <strong>Logged!</strong>
        <span>Saved to the server</span>
      </p>
    </div>
  );
}
