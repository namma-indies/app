import { useEffect, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type { PairLocation } from "../api";
import { basemapStyle } from "./DogMap";
import { cellRing, pairExtent } from "../pairWhere";

/** The two sightings of a match pair on one small map.
 *
 * Same basemap as the main map (DogMap), but not DogMap itself: that one is
 * the cohort map, with clustering, popups and report/block, none of which
 * belongs inside a card. This draws exactly two things and is not interactive
 * -- a card is for deciding "same dog or not", and the map is context.
 *
 * Another observer's sighting arrives as its grid cell and is drawn as that
 * square, never as a pin (see app/precision.py). The viewer's own sighting is
 * a pin at its exact point.
 *
 * A review queue can hold a hundred cards and browsers cap live WebGL
 * contexts at around sixteen, so a map exists only while its card is on
 * screen. Without IntersectionObserver there is no map, only the text line
 * the card shows anyway. */

export interface PairPoint {
  label: string;
  location: PairLocation;
}

const SOURCE = "pair-cells";

function pinEl(label: string): HTMLElement {
  const el = document.createElement("div");
  el.className = "pair-pin";
  el.textContent = label;
  return el;
}

function LiveMap({ points }: { points: PairPoint[] }) {
  const ref = useRef<HTMLDivElement>(null);
  // Keyed on the content, not the array: the card re-renders on every busy
  // flag and note, and a fresh array each time would rebuild the map.
  const key = JSON.stringify(points);
  useEffect(() => {
    if (!ref.current) return;
    let map: maplibregl.Map;
    try {
      map = new maplibregl.Map({
        container: ref.current,
        style: basemapStyle(),
        interactive: false,
        attributionControl: { compact: true },
        center: [points[0].location.lng, points[0].location.lat],
        zoom: 13,
      });
    } catch {
      // No WebGL: the text line under the map still says what matters.
      return;
    }
    const extent = pairExtent(points.map((p) => p.location));
    const bounds = new maplibregl.LngLatBounds();
    for (const c of extent) bounds.extend(c);
    map.fitBounds(bounds, { padding: 28, maxZoom: 15, duration: 0 });

    for (const p of points) {
      if (p.location.precision === "exact") {
        new maplibregl.Marker({ element: pinEl(p.label) })
          .setLngLat([p.location.lng, p.location.lat])
          .addTo(map);
      }
    }
    map.on("load", () => {
      const features = points.flatMap((p) => {
        const ring = cellRing(p.location);
        return ring ? [{
          type: "Feature" as const,
          properties: { label: p.label },
          geometry: { type: "Polygon" as const, coordinates: [ring] },
        }] : [];
      });
      if (!features.length) return;
      map.addSource(SOURCE, { type: "geojson", data: { type: "FeatureCollection", features } });
      map.addLayer({ id: `${SOURCE}-fill`, type: "fill", source: SOURCE,
        paint: { "fill-color": "#c15f3c", "fill-opacity": 0.18 } });
      map.addLayer({ id: `${SOURCE}-edge`, type: "line", source: SOURCE,
        paint: { "line-color": "#a44a2b", "line-width": 2, "line-dasharray": [2, 2] } });
      map.addLayer({ id: `${SOURCE}-label`, type: "symbol", source: SOURCE,
        layout: { "text-field": ["get", "label"], "text-size": 13 },
        paint: { "text-color": "#2e2016", "text-halo-color": "#fffdf8", "text-halo-width": 2 } });
    });
    return () => map.remove();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return <div ref={ref} className="pair-map-canvas" />;
}

export default function PairMap({ points, label }: { points: PairPoint[]; label: string }) {
  const frame = useRef<HTMLDivElement>(null);
  const [onScreen, setOnScreen] = useState(false);
  useEffect(() => {
    const el = frame.current;
    if (!el || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) setOnScreen(e.isIntersecting);
    }, { rootMargin: "200px 0px" });
    io.observe(el);
    return () => io.disconnect();
  }, []);
  if (!points.length) return null;
  return (
    <div ref={frame} className="pair-map" role="img" aria-label={label}>
      {onScreen && <LiveMap points={points} />}
    </div>
  );
}
