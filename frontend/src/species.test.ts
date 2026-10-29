import { describe, expect, it } from "vitest";
import { commonSpecies, speciesNoun, speciesPlural, speciesWithArticle } from "./species";

describe("species wording", () => {
  it("names the species, and 'animal' when it is unknown", () => {
    expect(speciesNoun("cat")).toBe("cat");
    expect(speciesNoun("dog")).toBe("dog");
    expect([speciesNoun(null), speciesNoun(undefined), speciesNoun("")]).toEqual(["animal", "animal", "animal"]);
    expect([speciesWithArticle("cat"), speciesWithArticle("dog"), speciesWithArticle(null)]).toEqual(["a cat", "a dog", "an animal"]);
    expect(speciesPlural("cat")).toBe("cats");
    expect(speciesPlural(null)).toBe("animals");
  });
  it("is one species only when every member agrees", () => {
    expect(commonSpecies(["cat", "cat"])).toBe("cat");
    expect(commonSpecies(["dog", "cat"])).toBeNull();
    expect(commonSpecies(["dog", null])).toBeNull();
    expect(commonSpecies([])).toBeNull();
  });
});
