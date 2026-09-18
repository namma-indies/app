// Placeholder community-cat sprite — wedge ears, upright tail, compact frame.
// Hand-plotted pixel map in the same idiom and grid as DogSprite, so the two
// can run alongside each other without looking like they came from different
// games. A real pixel artist replaces this later.
//
// "Mostly dogs, for now. Though the cats are here too."

export type CatCoat = "ginger" | "sooty" | "tabby" | "calico" | "smoke";

const COATS: Record<CatCoat, { B: string; S: string }> = {
  ginger: { B: "#d98a3f", S: "#a8602a" },
  sooty: { B: "#514336", S: "#332820" },
  tabby: { B: "#9b7a4e", S: "#6d5334" },
  calico: { B: "#ecdcc4", S: "#c08a52" },
  smoke: { B: "#a9a49b", S: "#7d7970" },
};

const OUTLINE = "#33241b";
const EYE = "#f7eee0";

// 18 wide × 14 tall, facing right like the dog, tail up at the left.
// '#' outline · 'B' coat · 'S' shade · 'o' eye.
const CAT = [
  "                  ",
  "            #  #  ",
  "  ##       #B##B# ",
  " #BB#     #BBBBBB#",
  " #BB#     #BBBoBB#",
  " #BB#     #BBBBBB#",
  " #BB#####BBBBBBBB#",
  " #BBBBBBBBBBBBBBB#",
  "  #BBBBBBBBBBBBBB#",
  "  #BSBBBBBBBBBB#  ",
  "  #BBBBBBBBBBBB#  ",
  "  #B##B##B##B#    ",
  "  ## ## ## ##     ",
  "                  ",
];

export default function CatSprite({
  coat = "ginger",
  scale = 6,
  className,
}: {
  coat?: CatCoat;
  scale?: number;
  className?: string;
}) {
  const pal = COATS[coat];
  const paint: Record<string, string> = {
    "#": OUTLINE,
    B: pal.B,
    S: pal.S,
    o: EYE,
  };
  const w = CAT[0].length;
  const h = CAT.length;
  const rects: JSX.Element[] = [];
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const ch = CAT[y][x];
      const fill = paint[ch];
      if (!fill) continue;
      rects.push(<rect key={`${x}-${y}`} x={x} y={y} width={1} height={1} fill={fill} />);
    }
  }
  return (
    <svg
      className={className}
      width={w * scale}
      height={h * scale}
      viewBox={`0 0 ${w} ${h}`}
      shapeRendering="crispEdges"
      aria-hidden="true"
      style={{
        imageRendering: "pixelated",
        filter: `drop-shadow(0 ${Math.max(1, scale * 0.5)}px 0 rgba(51,36,27,0.35))`,
      }}
    >
      {rects}
    </svg>
  );
}

export { CAT as CAT_PIXELS };
