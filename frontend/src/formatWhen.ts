/** The one way a sighting's date and time is written.
 *
 * Journal style -- `21 Oct · 3:45 pm` -- in the Indian locale, with no seconds.
 * Before this, the same instant read four different ways depending on the
 * screen (`9/1/2026, 12:36:07 AM`, `Taken 2/8/2025, 3:16:00 PM`,
 * `spotted Oct 21 · 3:45 PM`, `Oct 23, 2026`), and bare `toLocaleString()`
 * followed the device locale, so even one screen could disagree with itself.
 *
 * The year appears only when it is not the current one: this year's dates stay
 * short, and an old sighting is not left looking like it happened this year.
 *
 * The camera viewfinder's HUD timestamp deliberately does not use this -- it is
 * a camcorder overlay, not a record of when something happened.
 */
const LOCALE = "en-IN";

type When = string | number | Date;

function asDate(when: When): Date {
  return when instanceof Date ? when : new Date(when);
}

function dayOf(d: Date, now: Date): string {
  const opts: Intl.DateTimeFormatOptions = { day: "numeric", month: "short" };
  if (d.getFullYear() !== now.getFullYear()) opts.year = "numeric";
  return d.toLocaleDateString(LOCALE, opts);
}

/** `21 Oct` -- or `21 Oct 2025` when the year isn't the current one. */
export function formatDay(when: When, now: Date = new Date()): string {
  return dayOf(asDate(when), now);
}

/** `21 Oct · 3:45 pm` */
export function formatWhen(when: When, now: Date = new Date()): string {
  const d = asDate(when);
  const time = d.toLocaleTimeString(LOCALE, { hour: "numeric", minute: "2-digit", hour12: true });
  return `${dayOf(d, now)} · ${time}`;
}

/** A first-to-last span of calendar days (`YYYY-MM-DD`). The same day on both
 * ends is said once rather than as `Oct 23 → Oct 23`. */
export function formatDayRange(first: string, last: string, now: Date = new Date()): string {
  const a = formatDay(`${first}T00:00:00`, now);
  const b = formatDay(`${last}T00:00:00`, now);
  return a === b ? a : `${a} → ${b}`;
}
