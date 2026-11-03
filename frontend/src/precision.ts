/** Place-search bias uses the same deterministic grid as app/precision.py.
 * Its size comes from the server; an unavailable/disabled grid omits bias. */
export function coarsenPlaceBias(
  near: { lat: number; lng: number },
  cellM: number,
): { lat: number; lng: number } | null {
  if (!Number.isFinite(cellM) || cellM <= 0
    || !Number.isFinite(near.lat) || Math.abs(near.lat) > 90
    || !Number.isFinite(near.lng) || Math.abs(near.lng) > 180) return null;

  const latStep = cellM / 111_320;
  const latCentre = (Math.floor(near.lat / latStep) + 0.5) * latStep;
  // Use the snapped latitude so every point in one cell has the same width.
  const cosLat = Math.max(Math.cos(latCentre * Math.PI / 180), 0.01);
  const lngStep = cellM / (111_320 * cosLat);
  const lngCentre = (Math.floor(near.lng / lngStep) + 0.5) * lngStep;
  return {
    lat: Math.max(-90, Math.min(90, Number(latCentre.toFixed(5)))),
    lng: Math.max(-180, Math.min(180, Number(lngCentre.toFixed(5)))),
  };
}
