import { useEffect, useRef } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type { MappableSighting } from "../api";

const BANGALORE: [number, number] = [77.59, 12.97];
const SOURCE_ID = "sightings";

// CARTO basemaps: vector, no API key, free with attribution. Replaces the raw
// OSM raster tiles, which carried full road-atlas detail in colours that fought
// the warm paper palette -- the map competed with the sightings instead of
// sitting behind them. Positron/Dark Matter are deliberately desaturated so the
// data reads first; `.map-canvas` warms them toward the cream palette.
//
// Still not a production licence decision: CARTO's free tier has usage limits.
// A self-hosted Protomaps .pmtiles removes the third party entirely if that
// matters later -- tracked in GitHub issue #2.
const BASEMAP_LIGHT = "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json";
const BASEMAP_DARK = "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json";

function basemapStyle(): string {
  const dark =
    typeof window !== "undefined" &&
    window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  return dark ? BASEMAP_DARK : BASEMAP_LIGHT;
}

const TAG_LABELS: Record<string, string> = {
  male: "♂ male",
  female: "♀ female",
  left: "notched-left",
  right: "notched-right",
  healthy: "healthy",
  injured: "injured",
};

/** Full escape, including quotes: these values land in attributes as well as
 * text. The previous version escaped only `<` in the note, which stops a tag
 * but not an attribute break-out. */
export function esc(s: string): string {
  return s.replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

/** GeoJSON properties must be flat primitives -- anything nested is stringified
 * on the way through the tile pipeline, so the tags are flattened here rather
 * than passing `attrs` through as an object. */
function toFeature(s: MappableSighting): GeoJSON.Feature<GeoJSON.Point> {
  const attrs = s.attrs || {};
  const tags = ([attrs.sex, attrs.ear_notch, attrs.condition] as (string | undefined)[])
    .filter((v): v is string => !!v && v !== "unsure" && v !== "none")
    .map((v) => TAG_LABELS[v] ?? v);
  return {
    type: "Feature",
    geometry: { type: "Point", coordinates: [s.lng as number, s.lat as number] },
    properties: {
      id: s.id,
      thumb: s.photos[0]?.thumb_url ?? "",
      time: new Date(s.captured_at).toLocaleString(),
      note: attrs.note ?? "",
      tags: tags.join("|"),
      // Who logged it, shown on tapping a sighting and never on the pin
      // itself. Empty for the viewer's own sightings and for /dex responses,
      // where "who" is never in question.
      observer: s.mine === false && s.observer ? s.observer : "",
    },
  };
}

export function popupHtml(p: Record<string, string>): string {
  const tags = p.tags ? p.tags.split("|") : [];
  return `
    <div class="map-popup">
      ${p.thumb ? `<img src="${esc(p.thumb)}" alt="dog sighting" />` : ""}
      <div class="time">${esc(p.time)}</div>
      ${p.observer ? `<div class="popup-by">logged by ${esc(p.observer)}</div>` : ""}
      ${p.note ? `<div class="popup-note">${esc(p.note)}</div>` : ""}
      ${
        tags.length
          ? `<div class="popup-tags">${tags
              .map((t) => `<span class="tag">${esc(t)}</span>`)
              .join("")}</div>`
          : ""
      }
      ${
        // Only on someone else's sighting -- reporting your own is not a thing
        // anyone wants, and deleting it is a different feature. `p.id` is
        // required too: the button carries the id it would report, so without
        // one there is nothing for the delegated handler to act on.
        p.observer && p.id
          ? `<button type="button" class="popup-report" data-report="${esc(p.id)}">report</button>`
          : ""
      }
    </div>`;
}

function pinEl(p: Record<string, string>): HTMLElement {
  const el = document.createElement("div");
  el.className = "photo-pin";
  if (p.thumb) {
    const img = document.createElement("img");
    img.src = p.thumb;
    img.alt = "dog sighting";
    el.appendChild(img);
  } else {
    el.textContent = "🐾";
    el.classList.add("photo-pin-fallback");
  }
  return el;
}

function clusterEl(count: number): HTMLElement {
  const el = document.createElement("div");
  // Three sizes rather than a continuous scale: a bubble still has to hold a
  // legible number, so it can't shrink freely the way a photo could.
  const size = count < 10 ? "sm" : count < 50 ? "md" : "lg";
  el.className = `cluster-pin cluster-${size}`;
  el.innerHTML = `<span class="cluster-count">${count}</span><span class="cluster-paw">🐾</span>`;
  el.setAttribute("role", "button");
  el.setAttribute("aria-label", `${count} sightings — zoom in to separate them`);
  return el;
}

export default function DogMap({
  sightings,
  onReport,
}: {
  sightings: MappableSighting[];
  /** Called with a sighting id when someone taps `report` in its popup.
   * Omitted on surfaces where reporting makes no sense (your own dex). */
  onReport?: (sightingId: string) => void;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  // Read inside a listener registered once, which would otherwise close over
  // the first render's handler forever.
  const onReportRef = useRef(onReport);
  onReportRef.current = onReport;
  // Read inside map callbacks, which are registered once and would otherwise
  // close over the first render's sightings forever.
  const sightingsRef = useRef(sightings);
  sightingsRef.current = sightings;

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;

    const map = new maplibregl.Map({
      container: containerRef.current,
      style: basemapStyle(),
      center: BANGALORE,
      zoom: 11,
      attributionControl: { compact: true },
    });
    mapRef.current = map;
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");

    // One delegated listener on the container rather than wiring each popup as
    // it opens. Popups are created and destroyed constantly as markers come in
    // and out of view, and per-popup listeners leak with them.
    const container = containerRef.current;
    const onClick = (e: MouseEvent) => {
      const target = (e.target as HTMLElement | null)?.closest?.("[data-report]");
      const id = target?.getAttribute("data-report");
      if (id) onReportRef.current?.(id);
    };
    container.addEventListener("click", onClick);

    map.on("load", () => {
      map.addSource(SOURCE_ID, {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
        cluster: true,
        // Roughly two pin-widths, so photos merge only once they'd actually
        // overlap rather than while there's still room between them.
        clusterRadius: 70,
        // Past this zoom, show every sighting individually even if they stack:
        // at street level the overlap is the honest picture of the data.
        clusterMaxZoom: 17,
      });

      // querySourceFeatures only returns what's in loaded tiles, and tiles are
      // only loaded for sources some layer references. These exist purely to
      // pull the data through; the visible markers are DOM elements.
      map.addLayer({
        id: "sightings-hit",
        type: "circle",
        source: SOURCE_ID,
        paint: { "circle-radius": 1, "circle-opacity": 0 },
      });

      const markers: Record<string, maplibregl.Marker> = {};
      let onScreen: Record<string, maplibregl.Marker> = {};

      const updateMarkers = () => {
        const next: Record<string, maplibregl.Marker> = {};
        for (const f of map.querySourceFeatures(SOURCE_ID)) {
          const coords = (f.geometry as GeoJSON.Point).coordinates as [number, number];
          const props = (f.properties ?? {}) as Record<string, string> & {
            cluster?: boolean;
            cluster_id?: number;
            point_count?: number;
          };
          // Keyed so the same feature arriving from two overlapping tiles
          // reuses one marker instead of stacking duplicates.
          const key = props.cluster ? `c${props.cluster_id}` : `s${props.id}`;
          if (next[key]) continue;

          let marker = markers[key];
          if (!marker) {
            if (props.cluster) {
              const el = clusterEl(props.point_count ?? 0);
              const clusterId = props.cluster_id as number;
              el.addEventListener("click", () => {
                const src = map.getSource(SOURCE_ID) as maplibregl.GeoJSONSource;
                // Zoom to exactly where this cluster splits -- guessing a
                // zoom step either overshoots or leaves it clustered.
                src
                  .getClusterExpansionZoom(clusterId)
                  .then((zoom) => map.easeTo({ center: coords, zoom, duration: 500 }))
                  .catch(() => {});
              });
              marker = new maplibregl.Marker({ element: el }).setLngLat(coords);
            } else {
              marker = new maplibregl.Marker({ element: pinEl(props) })
                .setLngLat(coords)
                .setPopup(new maplibregl.Popup({ offset: 18 }).setHTML(popupHtml(props)));
            }
            markers[key] = marker;
          }
          next[key] = marker;
          if (!onScreen[key]) marker.addTo(map);
        }
        for (const key of Object.keys(onScreen)) {
          if (!next[key]) onScreen[key].remove();
        }
        onScreen = next;
      };

      map.on("render", () => {
        if (map.isSourceLoaded(SOURCE_ID)) updateMarkers();
      });

      applyData(map, sightingsRef.current, true);
    });

    return () => {
      container.removeEventListener("click", onClick);
      map.remove();
      mapRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.isStyleLoaded() || !map.getSource(SOURCE_ID)) return;
    applyData(map, sightings, false);
  }, [sightings]);

  return <div ref={containerRef} className="map-wrap" />;
}

/** Push the current sightings into the source, and on first load frame them.
 * Fitting only once is deliberate: re-framing on every refresh would yank the
 * map out from under someone who had panned somewhere deliberately. */
function applyData(map: maplibregl.Map, sightings: MappableSighting[], fit: boolean): void {
  const geoed = sightings.filter((s) => s.lat != null && s.lng != null);
  const src = map.getSource(SOURCE_ID) as maplibregl.GeoJSONSource | undefined;
  if (!src) return;
  src.setData({ type: "FeatureCollection", features: geoed.map(toFeature) });

  if (!fit || geoed.length === 0) return;
  if (geoed.length === 1) {
    map.jumpTo({ center: [geoed[0].lng as number, geoed[0].lat as number], zoom: 15 });
    return;
  }
  const bounds = new maplibregl.LngLatBounds();
  for (const s of geoed) bounds.extend([s.lng as number, s.lat as number]);
  // maxZoom stops a tight cluster of sightings from slamming to street level.
  map.fitBounds(bounds, { padding: 56, maxZoom: 15, duration: 0 });
}
