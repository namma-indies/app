/** How an identified animal is referred to, everywhere.
 *
 * The number is theirs for good (`individuals.number`, migration 0050): the
 * same on their card, in the Journal, in a recognition and in a verdict's
 * confirmation, and the same for every person looking. It used to be a card's
 * position in one screen's list, which is why this lives in one place now.
 *
 * Until naming ships (issue #4) the number is the identity. A name, once
 * there is one, replaces it wherever the animal is mentioned. */

export interface Identified {
  name: string | null;
  number: number;
}

/** `NO. 047` -- for use inside a sentence. */
export function dogNumber(n: number): string {
  return `NO. ${String(n).padStart(3, "0")}`;
}

/** `Kalu`, or `NO. 047` -- for use inside a sentence. */
export function dogName(d: Identified): string {
  return d.name ?? dogNumber(d.number);
}

/** `Kalu`, or `— NO. 047 —` -- the title on a card. The dashes mark a number
 * standing where a name will be. */
export function dogTitle(d: Identified): string {
  return d.name ?? `— ${dogNumber(d.number)} —`;
}
