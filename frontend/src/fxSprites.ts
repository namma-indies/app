import type { CSSProperties } from "react";

// Pixel sprites for the voice-note suggestion comet (#136), in the same
// hand-plotted idiom as DogSprite and CatSprite: a grid of characters, one
// per pixel, rendered crisp. Original drawings, no external assets.
//
// '#' outline (the theme's --line, so it reads on cream and on bark) and a
// few fills per sprite. A space or '.' is transparent.

export type Species = "dog" | "cat";

export interface Sprite { grid: string[]; paint: Record<string, string> }

/** A bone, for a dog: the comet tumbles end over end, like a thrown stick. */
export const BONE: Sprite = {
  grid: [
    ".##.......##.",
    "#WW#.....#WW#",
    "#WWW#####WWW#",
    ".#WWWWWWWWS#.",
    "#WWS#####SSS#",
    "#WS#.....#SS#",
    ".##.......##.",
  ],
  paint: { "#": "currentColor", W: "#f6ead2", S: "#d4bc94" },
};

/** A fish, for a cat: swims through the air, nose first. Faces right. */
export const FISH: Sprite = {
  grid: [
    "....#####...",
    "#..#FFFFF#..",
    "#F#FFFFFoF#.",
    ".#FFFFFFFFF#",
    "#F#FFFFFFF#.",
    "#..#SSSSS#..",
    "....#####...",
  ],
  paint: { "#": "currentColor", F: "#8fd0e8", S: "#4f9fc0", o: "#1d1410" },
};

/** A sparkle, for an animal whose species isn't known. */
export const SPARKLE: Sprite = {
  grid: [
    "...Y...",
    "...Y...",
    "..YWY..",
    "YYWWWYY",
    "..YWY..",
    "...Y...",
    "...Y...",
  ],
  paint: { Y: "var(--fx-1)", W: "#fffaf0" },
};

/** A cat's print: four round toes and a three-lobed pad, no claws (they're tucked in). Toes up. */
export const CAT_PAW = [
  "...........",
  "...PP.PP...",
  "PP.PP.PP.PP",
  "PP.......PP",
  "....PPP....",
  "..PPPPPPP..",
  ".PPPPPPPPP.",
  ".PPPPPPPPP.",
  "..PPP.PPP..",
];

/** A dog's print: the same, with the claw marks a dog can't tuck in. Toes up. */
export const DOG_PAW = [
  "...P...P...",
  "P..PP.PP..P",
  "PP.PP.PP.PP",
  "PP.......PP",
  "....PPP....",
  "..PPPPPPP..",
  ".PPPPPPPPP.",
  ".PPPPPPPPP.",
  "..PPP.PPP..",
];

/** The trail and stamp for an unknown species: a small four-point twinkle. */
export const TWINKLE = [
  "..P..",
  "..P..",
  "PPPPP",
  "..P..",
  "..P..",
];

export const comet = (species: Species | null): Sprite => species === "dog" ? BONE : species === "cat" ? FISH : SPARKLE;
export const print = (species: Species | null): string[] => species === "dog" ? DOG_PAW : species === "cat" ? CAT_PAW : TWINKLE;

/** What the animal says as its comet lands, in turn. An unknown animal says nothing. */
export const SOUNDS: Record<Species, string[]> = {
  cat: ["mrrp", "prrt", "mew"],
  dog: ["woof", "arf", "boof"],
};

/** A grid as SVG markup, `cell` px per pixel. Runs of one colour are merged into one rect. */
export function pixelSvg(grid: string[], paint: Record<string, string>, cell: number): string {
  const w = grid[0].length, h = grid.length;
  let rects = "";
  grid.forEach((row, y) => {
    for (let x = 0; x < w;) {
      const fill = paint[row[x]];
      let end = x + 1;
      while (end < w && row[end] === row[x]) end++;
      if (fill) rects += `<rect x="${x}" y="${y}" width="${end - x}" height="1" style="fill:${fill}"/>`;
      x = end;
    }
  });
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${w * cell}" height="${h * cell}" viewBox="0 0 ${w} ${h}" shape-rendering="crispEdges">${rects}</svg>`;
}

/** A one-colour grid as a data: URI, for a CSS mask (the stamp takes its colours from the theme). */
export const pixelMask = (grid: string[]) =>
  `url("data:image/svg+xml,${encodeURIComponent(pixelSvg(grid, { P: "#000" }, 1)).replace(/%20/g, " ").replace(/%3D/g, "=").replace(/%3A/g, ":").replace(/%2F/g, "/").replace(/%22/g, "'")}")`;

/**
 * The props for an element around one animal's suggestions: which species its
 * comets are (data-fx-species, read by suggestionFlow) and the paw stamp its
 * suggested chips wear (--fx-stamp, in suggestionFlow.css). Anything not a dog
 * or a cat gets the twinkle.
 */
export function fxScope(species?: string | null): { "data-fx-species": string; style: CSSProperties } {
  const known = species === "dog" || species === "cat" ? species : null;
  return { "data-fx-species": known ?? "unknown", style: { "--fx-stamp": pixelMask(print(known)) } as CSSProperties };
}
