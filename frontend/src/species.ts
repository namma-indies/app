/** Wording for one animal, or a group of them, by species.
 *
 * Species is "dog", "cat" or unknown (null / undefined). Unknown reads as
 * "animal" so nothing ever calls a cat a dog. A group counts as one species
 * only when every member agrees; any mix, or any unknown member, is neutral. */
export type Species = "dog" | "cat";

export const isSpecies = (value: unknown): value is Species => value === "dog" || value === "cat";

/** "cat" | "dog" | "animal". */
export function speciesNoun(species?: string | null): string {
  return isSpecies(species) ? species : "animal";
}

/** "cats" | "dogs" | "animals". */
export function speciesPlural(species?: string | null): string {
  return `${speciesNoun(species)}s`;
}

/** "a cat" | "a dog" | "an animal". */
export function speciesWithArticle(species?: string | null): string {
  const noun = speciesNoun(species);
  return `${noun === "animal" ? "an" : "a"} ${noun}`;
}

/** The shared species of a group, or null when it is mixed, unknown or empty. */
export function commonSpecies(group: Iterable<string | null | undefined>): Species | null {
  const kinds = new Set(group);
  const [only] = kinds;
  return kinds.size === 1 && isSpecies(only) ? only : null;
}
