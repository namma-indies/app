import CatSprite, { type CatCoat } from "./CatSprite";
import DogSprite, { type Coat } from "./DogSprite";
import type { Streak } from "../streak";

/**
 * The card for a hot streak (#81): made to be screenshotted and sent.
 *
 * So everything that matters fits one portrait screen with nothing below the
 * fold, and the name and date are part of the picture, so a crop that loses
 * the status bar still says who and when.
 *
 * What it leaves out is as deliberate as what it shows. No map and no place:
 * your own sightings are exact (app/precision.py only coarsens other
 * people's), and a card built to leave the cohort must not carry the exact
 * spots where street animals were. And it counts sightings, never dogs:
 * detection and matching run later and may find nothing.
 */

const DOG_COATS: Coat[] = ["tan", "black", "piebald", "brindle", "ghost"];
const CAT_COATS: CatCoat[] = ["ginger", "sooty", "tabby", "calico", "smoke"];
const MAX_SPRITES = 10;

function minutesBetween(streak: Streak): number {
  return Math.max(1, Math.round((streak.lastAt.getTime() - streak.firstAt.getTime()) / 60000));
}

export default function StreakCard({ streak, onClose }: { streak: Streak; onClose: () => void }) {
  const minutes = minutesBetween(streak);
  const day = streak.lastAt.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  const time = (d: Date) => d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  const sprites = Math.min(streak.count, MAX_SPRITES);
  // Big enough to read in a shared screenshot, small enough that ten fit.
  const scale = sprites <= 6 ? 3 : 2;

  return (
    <div className="streak-overlay" onClick={onClose}>
      <section
        className="streak-card"
        role="dialog"
        aria-modal="true"
        aria-label="Hot streak"
        onClick={(e) => e.stopPropagation()}
      >
        <p className="streak-brand">NAMMA INDIES</p>
        <p className="spot-label">HOT STREAK</p>
        <p className="streak-count">{streak.count}</p>
        <p className="streak-what">
          sightings logged in {minutes} {minutes === 1 ? "minute" : "minutes"}
        </p>
        <div className="streak-pack" aria-hidden="true">
          {Array.from({ length: sprites }, (_, i) =>
            i % 3 === 1 ? (
              <CatSprite key={i} coat={CAT_COATS[i % CAT_COATS.length]} scale={scale} />
            ) : (
              <DogSprite key={i} coat={DOG_COATS[i % DOG_COATS.length]} scale={scale} />
            ),
          )}
        </div>
        <p className="streak-when">
          {day} · {time(streak.firstAt)}–{time(streak.lastAt)}
        </p>
        <p className="streak-invite">Spotted with Namma Indies · app.nammaindies.org</p>
        <button type="button" className="btn btn-primary streak-close" onClick={onClose}>
          NICE
        </button>
      </section>
    </div>
  );
}
