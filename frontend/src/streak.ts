/**
 * A hot streak: several sightings logged in one short stretch of a walk (#81).
 *
 * Counted on this device, from the capture times of uploads the server has
 * acknowledged. Three choices, each to keep the card honest:
 *
 * - Acknowledged, not queued. The card says "logged", so it may only count
 *   what the server has actually durably saved, exactly like the celebration.
 * - Capture time, not upload time. "Five in fifteen minutes" means a good
 *   walk. Upload time would turn a week's offline backlog, flushed in one
 *   second when signal returns, into a fake streak.
 * - On the device, nothing stored on the server. A personal "best walk" is a
 *   per-person number, and whether those become stored, comparable things is
 *   the open question in #58/#5 (`/stats/observers` is moderator-only for
 *   that reason). Here it is a private readout the person screenshots.
 */

/** The one place the threshold lives. */
export const STREAK = { count: 5, windowMinutes: 15 } as const;

/** A walk that ended longer ago than this, uploaded late, is history now,
 *  not a moment. It still counts towards the log; it just gets no card. */
const FRESH_MS = 12 * 60 * 60 * 1000;
/** After a card, the walk carries on; the next card needs a real gap. */
const COOLDOWN_MS = 60 * 60 * 1000;
const KEEP_MS = 24 * 60 * 60 * 1000;
const MAX_KEPT = 200;

const RECENT_KEY = "indiedex:recentCaptures";
const LAST_CARD_KEY = "indiedex:lastStreakCard";

export interface Streak {
  count: number;
  firstAt: Date;
  lastAt: Date;
}

function read(key: string): unknown {
  try {
    const raw = localStorage.getItem(key);
    return raw === null ? null : JSON.parse(raw);
  } catch {
    // Private browsing, a cleared or blocked store: no streak, never an error.
    return null;
  }
}

function write(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Losing this loses a card, not a sighting.
  }
}

/**
 * Note one acknowledged sighting. Returns the streak it completes, once per
 * run, or null.
 */
export function recordAcknowledged(capturedAt: string | undefined, now = Date.now()): Streak | null {
  const at = capturedAt ? Date.parse(capturedAt) : NaN;
  if (!Number.isFinite(at)) return null;

  const stored = read(RECENT_KEY);
  const kept = (Array.isArray(stored) ? stored : [])
    .filter((t): t is number => typeof t === "number" && now - t <= KEEP_MS);
  kept.push(at);
  kept.sort((a, b) => a - b);
  write(RECENT_KEY, kept.slice(-MAX_KEPT));

  if (now - at > FRESH_MS) return null;
  const windowMs = STREAK.windowMinutes * 60 * 1000;
  const run = kept.filter((t) => t > at - windowMs && t <= at);
  if (run.length < STREAK.count) return null;

  const lastCard = read(LAST_CARD_KEY);
  if (typeof lastCard === "number" && at - lastCard < COOLDOWN_MS) return null;
  write(LAST_CARD_KEY, at);
  return { count: run.length, firstAt: new Date(run[0]), lastAt: new Date(at) };
}
