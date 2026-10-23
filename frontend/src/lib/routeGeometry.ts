import type { DurationBreakpoint } from "../api/types";

const EARTH_RADIUS_MI = 3958.8;

function toRad(deg: number): number {
  return (deg * Math.PI) / 180;
}

function haversineMi([lat1, lon1]: [number, number], [lat2, lon2]: [number, number]): number {
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * EARTH_RADIUS_MI * Math.asin(Math.sqrt(h));
}

export function cumulativeDistances(polyline: [number, number][]): number[] {
  const cumulative = [0];
  for (let i = 1; i < polyline.length; i++) {
    cumulative.push(cumulative[cumulative.length - 1] + haversineMi(polyline[i - 1], polyline[i]));
  }
  return cumulative;
}

function bearingDeg([lat1, lon1]: [number, number], [lat2, lon2]: [number, number]): number {
  const y = Math.sin(toRad(lon2 - lon1)) * Math.cos(toRad(lat2));
  const x =
    Math.cos(toRad(lat1)) * Math.sin(toRad(lat2)) -
    Math.sin(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.cos(toRad(lon2 - lon1));
  return ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
}

// Point + local heading at target_mi along the route, linearly interpolated
// between the bracketing polyline vertices — a port of the backend's
// routing/checkpoints.py::locate_along_route, so the truck marker lands on
// the same line the backend already computed checkpoints from.
function locateAlongRoute(
  polyline: [number, number][],
  cumulative: number[],
  targetMi: number,
): { lat: number; lon: number; bearingDeg: number } {
  for (let i = 0; i < cumulative.length - 1; i++) {
    const segStart = cumulative[i];
    const segEnd = cumulative[i + 1];
    if (segStart <= targetMi && targetMi <= segEnd) {
      const a = polyline[i];
      const b = polyline[i + 1];
      const bearing = bearingDeg(a, b);
      if (segEnd === segStart) return { lat: a[0], lon: a[1], bearingDeg: bearing };
      const frac = (targetMi - segStart) / (segEnd - segStart);
      return { lat: a[0] + frac * (b[0] - a[0]), lon: a[1] + frac * (b[1] - a[1]), bearingDeg: bearing };
    }
  }
  const last = polyline[polyline.length - 1];
  const prev = polyline.length > 1 ? polyline[polyline.length - 2] : last;
  return { lat: last[0], lon: last[1], bearingDeg: bearingDeg(prev, last) };
}

export interface RoutePosition {
  lat: number;
  lon: number;
  bearingDeg: number;
  state: "waiting" | "enroute" | "arrived";
}

// Maps elapsed time -> distance via duration_breakpoints (built from OSRM's
// own per-segment durations), then walks the polyline to that distance the
// same way the backend's checkpoint/heatmap sampling does. Two different
// distance measures meet here (OSRM-reported vs. haversine-over-vertices),
// but that's the same cross-reference the backend's own ETA computation
// already relies on (routing/eta.py reads checkpoint distances, themselves
// haversine-cumulative, against these OSRM-distance-cumulative
// breakpoints) — not a new inconsistency this introduces.
export function positionAtHour(
  polyline: [number, number][],
  breakpoints: DurationBreakpoint[],
  hours: number,
): RoutePosition | null {
  if (polyline.length === 0 || breakpoints.length === 0) return null;
  const targetS = hours * 3600;
  const totalS = breakpoints[breakpoints.length - 1].cumulative_duration_s;

  let targetMi: number;
  let state: RoutePosition["state"];
  if (targetS <= 0) {
    targetMi = 0;
    state = "waiting";
  } else if (targetS >= totalS) {
    targetMi = breakpoints[breakpoints.length - 1].distance_mi;
    state = "arrived";
  } else {
    state = "enroute";
    let i = 0;
    while (i < breakpoints.length - 1 && breakpoints[i + 1].cumulative_duration_s < targetS) i++;
    const a = breakpoints[i];
    const b = breakpoints[Math.min(i + 1, breakpoints.length - 1)];
    const span = b.cumulative_duration_s - a.cumulative_duration_s;
    const frac = span === 0 ? 0 : (targetS - a.cumulative_duration_s) / span;
    targetMi = a.distance_mi + frac * (b.distance_mi - a.distance_mi);
  }

  const cumulative = cumulativeDistances(polyline);
  const point = locateAlongRoute(polyline, cumulative, targetMi);
  return { ...point, state };
}
