import { useEffect, useLayoutEffect, useRef, type MutableRefObject } from "react";
import {
  LngLatBounds,
  Map as MapLibreMap,
  Marker,
  NavigationControl,
  Popup,
  type ExpressionSpecification,
  type GeoJSONSource,
  type MapLayerMouseEvent,
  type MapMouseEvent,
} from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import "../App.css"; // .map-view / .truck-marker* — raw-DOM marker styles MUI's theme can't reach
import type { FeatureCollection, Point } from "geojson";
import type { Heatmap, HeatmapLayer, Location, RiskLevel, RouteResult } from "../api/types";
import { HEATMAP_CLOUD_STYLES, valuesForLayer } from "../lib/heatmapStyles";
import { RISK_COLORS, RISK_LABELS } from "../lib/riskColors";
import { cumulativeDistances, positionAtHour, type RoutePosition } from "../lib/routeGeometry";
import { useTripUIStore } from "../store/useTripUIStore";

// OpenFreeMap: free, unlimited, no API key — avoids a third credential
// for a demo basemap.
const STYLE_URL = "https://tiles.openfreemap.org/styles/liberty";

const ROUTE_COLOR_HIGHLIGHTED = "#1565c0";
const ROUTE_COLOR_DEFAULT = "#9e9e9e";
const ROUTE_WIDTH_HIGHLIGHTED = 6;
const ROUTE_WIDTH_DEFAULT = 4;

// Blend radius grows with zoom so the corridor keeps reading as one
// continuous cloud instead of fragmenting into separate blobs as the user
// zooms in and the same real-world gap between sampled points spans more
// screen pixels (SAMPLES_PER_ROUTE is fixed regardless of trip length, so
// that gap grows with both trip length and zoom).
const CORRIDOR_CLOUD_RADIUS = ["interpolate", ["linear"], ["zoom"], 0, 18, 9, 55] as unknown as number;
const CORRIDOR_CLOUD_INTENSITY = ["interpolate", ["linear"], ["zoom"], 0, 1, 9, 2.5] as unknown as number;

const ROUTE_RISK_COLOR_EXPRESSION = [
  "match",
  ["get", "level"],
  "low",
  RISK_COLORS.low,
  "moderate",
  RISK_COLORS.moderate,
  "high",
  RISK_COLORS.high,
  "severe",
  RISK_COLORS.severe,
  "no_travel",
  RISK_COLORS.no_travel,
  "#888888",
] as unknown as string;

interface MapViewProps {
  routes: RouteResult[];
  recommendedRouteId: string | null;
  pickedOrigin: Location | null;
  pickedDestination: Location | null;
  onMapPick: (coordinate: { lat: number; lon: number }) => void;
  heatmap: Heatmap | null;
}

function emptyFeatureCollection(): FeatureCollection {
  return { type: "FeatureCollection", features: [] };
}

function routesToFeatureCollection(routes: RouteResult[]): FeatureCollection {
  return {
    type: "FeatureCollection",
    features: routes.map((route) => ({
      type: "Feature",
      properties: { routeId: route.route_id },
      geometry: {
        type: "LineString",
        coordinates: route.polyline.map(([lat, lon]) => [lon, lat]),
      },
    })),
  };
}

function checkpointsToFeatureCollection(routes: RouteResult[]): FeatureCollection {
  return {
    type: "FeatureCollection",
    features: routes.flatMap((route) =>
      route.checkpoints.map((checkpoint) => ({
        type: "Feature" as const,
        properties: {
          routeId: route.route_id,
          sequence: checkpoint.sequence,
          distanceMi: checkpoint.distance_mi,
          etaUtc: checkpoint.eta_utc,
          level: checkpoint.risk?.level ?? "unknown",
          windLevel: checkpoint.risk?.wind_level ?? "unknown",
          rainLevel: checkpoint.risk?.rain_level ?? "unknown",
          snowLevel: checkpoint.risk?.snow_level ?? "unknown",
          windMph: checkpoint.weather?.wind_mph ?? null,
          rainInHr: checkpoint.weather?.rain_in_hr ?? null,
          snowInHr: checkpoint.weather?.snow_in_hr ?? null,
        },
        geometry: { type: "Point" as const, coordinates: [checkpoint.lon, checkpoint.lat] },
      })),
    ),
  };
}

// A risk-colored ribbon drawn alongside the route (via line-offset), never
// instead of it — routes-line keeps its own color/click behavior completely
// untouched, so route selection can't regress no matter what this layer
// does. Walks the real polyline (not a simplified approximation — see
// docs/phase-4-routing-performance.md §4.5 on why that would move
// checkpoint-adjacent positions by miles) and groups consecutive vertices
// in the same inter-checkpoint bracket into one LineString per run, so
// feature count stays bounded by checkpoint count, not vertex count.
function routeRiskSegments(routes: RouteResult[]): FeatureCollection {
  const features: FeatureCollection["features"] = [];

  for (const route of routes) {
    const { polyline, checkpoints } = route;
    if (polyline.length < 2 || checkpoints.length < 2) continue;

    const cumulative = cumulativeDistances(polyline);
    const polylineTotal = cumulative[cumulative.length - 1];
    if (polylineTotal <= 0) continue;
    const scale = route.distance_mi / polylineTotal;

    let bracket = 0;
    const levelAtMi = (distanceMi: number): string => {
      while (bracket < checkpoints.length - 2 && distanceMi > checkpoints[bracket + 1].distance_mi) {
        bracket++;
      }
      return checkpoints[bracket + 1]?.risk?.level ?? checkpoints[bracket]?.risk?.level ?? "unknown";
    };

    let runLevel: string | null = null;
    let runCoords: [number, number][] = [];
    const flushRun = () => {
      if (runLevel && runCoords.length >= 2) {
        features.push({
          type: "Feature",
          properties: { routeId: route.route_id, level: runLevel },
          geometry: { type: "LineString", coordinates: runCoords },
        });
      }
    };

    for (let i = 0; i < polyline.length; i++) {
      const distanceMi = cumulative[i] * scale;
      const level = levelAtMi(distanceMi);
      const [lat, lon] = polyline[i];
      if (level !== runLevel) {
        flushRun();
        // Carry the boundary point into the new run so adjacent runs share
        // a vertex and the ribbon never visibly gaps at a color change.
        runCoords = runCoords.length ? [runCoords[runCoords.length - 1], [lon, lat]] : [[lon, lat]];
        runLevel = level;
      } else {
        runCoords.push([lon, lat]);
      }
    }
    flushRun();
  }

  return { type: "FeatureCollection", features };
}

// One glyph per checkpoint that isn't plain "low" risk, picking whichever
// hazard(s) actually drove that checkpoint's level (routing/risk.py's
// `reasons`, already computed server-side) — skips calm checkpoints
// entirely rather than drawing a glyph everywhere, since those already
// read fine as a plain colored dot.
const CONDITION_EMOJI: Record<"wind" | "rain" | "snow", string> = { wind: "💨", rain: "🌧️", snow: "❄️" };

function weatherGlyphFeatureCollection(routes: RouteResult[]): FeatureCollection {
  return {
    type: "FeatureCollection",
    features: routes.flatMap((route) =>
      route.checkpoints.flatMap((checkpoint) => {
        const risk = checkpoint.risk;
        const condition = risk && risk.level !== "low" ? risk.reasons[0] : undefined;
        if (!condition) return [];
        return [
          {
            type: "Feature" as const,
            properties: { condition },
            geometry: { type: "Point" as const, coordinates: [checkpoint.lon, checkpoint.lat] },
          },
        ];
      }),
    ),
  };
}

// Renders an emoji to a small raster image MapLibre can use as a symbol
// icon. WebGL symbol layers need a registered image/glyph, and most vector
// map styles (this app's included) don't bundle color-emoji glyph ranges —
// rendering through a 2D canvas uses the browser's own font stack instead,
// which reliably supports color emoji everywhere a popup's plain HTML does.
function emojiToImageData(emoji: string, size = 48): ImageData {
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d")!;
  ctx.font = `${size * 0.75}px sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(emoji, size / 2, size / 2 + size * 0.04);
  return ctx.getImageData(0, 0, size, size);
}

function heatmapToFeatureCollection(
  heatmap: Heatmap | null,
  hour: number,
  layer: HeatmapLayer | null,
): FeatureCollection {
  if (!heatmap || !layer) return emptyFeatureCollection();
  // hour_offsets is always a contiguous [0..N] array from the backend, so
  // the hour value doubles as the index into each *_by_hour array directly.
  return {
    type: "FeatureCollection",
    features: heatmap.points.flatMap((point) => {
      const value = valuesForLayer(point, layer)[hour];
      if (value == null) return [];
      return [
        {
          type: "Feature" as const,
          properties: { v: value },
          geometry: { type: "Point" as const, coordinates: [point.lon, point.lat] },
        },
      ];
    }),
  };
}

function computeBounds(routes: RouteResult[], extraPoints: Location[]): LngLatBounds | null {
  if (routes.length === 0 && extraPoints.length === 0) return null;
  const bounds = new LngLatBounds();
  for (const route of routes) {
    for (const [lat, lon] of route.polyline) bounds.extend([lon, lat]);
  }
  for (const point of extraPoints) bounds.extend([point.lon, point.lat]);
  return bounds;
}

function popupRow(emoji: string, label: string, value: string, level: unknown): string {
  const levelKey = typeof level === "string" && level in RISK_COLORS ? (level as RiskLevel) : null;
  const badge = levelKey
    ? `<span style="color:${RISK_COLORS[levelKey]};font-weight:600;">${RISK_LABELS[levelKey]}</span>`
    : "";
  return `
    <div style="display:flex;align-items:center;gap:0.4em;padding:0.15em 0;">
      <span aria-hidden="true">${emoji}</span>
      <span style="color:#666;">${label}</span>
      <span style="margin-left:auto;font-variant-numeric:tabular-nums;">${value}</span>
      ${badge}
    </div>
  `;
}

function checkpointPopupHtml(properties: Record<string, unknown>): string {
  const { sequence, distanceMi, etaUtc, level, windLevel, rainLevel, snowLevel, windMph, rainInHr, snowInHr } =
    properties;
  const overallLevel = typeof level === "string" && level in RISK_COLORS ? (level as RiskLevel) : null;

  const weatherRows =
    windMph != null
      ? popupRow("💨", "Wind", `${Number(windMph).toFixed(0)} mph`, windLevel) +
        popupRow("🌧️", "Rain", `${Number(rainInHr).toFixed(2)} in/hr`, rainLevel) +
        popupRow("❄️", "Snow", `${Number(snowInHr).toFixed(2)} in/hr`, snowLevel)
      : `<p style="margin:0.3em 0 0;color:#666;">Forecast unavailable for this checkpoint</p>`;

  return `
    <div style="min-width:180px;">
      <strong>Checkpoint ${String(sequence)}</strong><br/>
      <span style="color:#666;">${Number(distanceMi).toFixed(1)} mi · ETA ${new Date(String(etaUtc)).toLocaleString()}</span>
      ${overallLevel ? `<div style="margin-top:0.3em;font-weight:700;color:${RISK_COLORS[overallLevel]};">${RISK_LABELS[overallLevel]}</div>` : ""}
      <div style="margin-top:0.2em;border-top:1px solid #e5e5e5;padding-top:0.2em;">
        ${weatherRows}
      </div>
    </div>
  `;
}

export function MapView({
  routes,
  recommendedRouteId,
  pickedOrigin,
  pickedDestination,
  onMapPick,
  heatmap,
}: MapViewProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const popupRef = useRef<Popup | null>(null);
  const originMarkerRef = useRef<Marker | null>(null);
  const destinationMarkerRef = useRef<Marker | null>(null);
  const truckMarkersRef = useRef<Map<string, Marker>>(new Map());
  const selectedRouteId = useTripUIStore((state) => state.selectedRouteId);
  const selectRoute = useTripUIStore((state) => state.selectRoute);
  const pickingField = useTripUIStore((state) => state.pickingField);
  const forecastHour = useTripUIStore((state) => state.forecastHour);
  const heatmapLayer = useTripUIStore((state) => state.heatmapLayer);

  // The mount effect's click handler needs the latest onMapPick, but must
  // stay registered only once — a ref sidesteps re-registering it on every
  // render without re-running the whole map-creation effect. Updated in a
  // layout effect (not during render) so a click can never read a stale
  // callback from before paint.
  const onMapPickRef = useRef(onMapPick);
  useLayoutEffect(() => {
    onMapPickRef.current = onMapPick;
  });

  // Map instance is created once and torn down on unmount; it is never
  // recreated when `routes` changes (see the data effect below).
  useEffect(() => {
    if (!containerRef.current) return;

    const map = new MapLibreMap({
      container: containerRef.current,
      style: STYLE_URL,
      center: [-98.5, 39.8],
      zoom: 3,
    });
    map.addControl(new NavigationControl(), "top-right");
    mapRef.current = map;

    map.on("click", (event: MapMouseEvent) => {
      if (!useTripUIStore.getState().pickingField) return;
      onMapPickRef.current({ lat: event.lngLat.lat, lon: event.lngLat.lng });
      useTripUIStore.getState().setPickingField(null);
    });

    map.on("load", () => {
      // Added before "routes"/"checkpoints" so it renders underneath them.
      //
      // Discrete blurred circles per sampled point, not MapLibre's native
      // "heatmap" (density/KDE) layer type: a continuous density surface
      // visually implies coverage between samples that was never actually
      // measured. This is ~30-100 points along a corridor, not an area
      // scan — one blurred mark per real sample is an honest rendering of
      // that; a smoothed KDE isn't. Color/opacity are set per selected
      // layer (risk/wind/rain/snow) in the heatmap data effect below;
      // "risk" here is just the initial paint before that first runs.
      // A real blended density surface (MapLibre's native "heatmap" layer
      // type, not a circle layer), covering the corridor as one continuous
      // cloud rather than discrete per-sample dots. heatmap-weight (set per
      // layer below) is what keeps this honest about severity — it's each
      // point's own real reading, normalized against that layer's actual
      // thresholds, not a free-floating visual density invented for effect.
      map.addSource("heatmap", { type: "geojson", data: emptyFeatureCollection() });
      map.addLayer({
        id: "corridor-heatmap-cloud",
        type: "heatmap",
        source: "heatmap",
        paint: {
          "heatmap-weight": HEATMAP_CLOUD_STYLES.risk.weight as number,
          "heatmap-color": HEATMAP_CLOUD_STYLES.risk.color as ExpressionSpecification,
          "heatmap-radius": CORRIDOR_CLOUD_RADIUS,
          "heatmap-intensity": CORRIDOR_CLOUD_INTENSITY,
          "heatmap-opacity": 0.8,
        },
      });

      map.addSource("routes", { type: "geojson", data: emptyFeatureCollection() });
      map.addLayer({
        id: "routes-line",
        type: "line",
        source: "routes",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": ROUTE_COLOR_DEFAULT, "line-width": ROUTE_WIDTH_DEFAULT },
      });

      // Risk-colored ribbon alongside the route (line-offset), never on top
      // of or instead of it — routes-line above is never hidden or
      // recolored by this layer, so clicking/selecting a route can't be
      // affected by whether this ribbon happens to be visible.
      map.addSource("route-risk", { type: "geojson", data: emptyFeatureCollection() });
      map.addLayer({
        id: "routes-risk-line",
        type: "line",
        source: "route-risk",
        layout: { "line-cap": "round", "line-join": "round", visibility: "none" },
        paint: {
          "line-color": ROUTE_RISK_COLOR_EXPRESSION,
          "line-width": 3,
          "line-offset": 5,
          "line-opacity": 0.9,
        },
      });

      // The selected/recommended route always needs to read clearly even
      // where its path happens to coincide with another route's — "routes"
      // is one GeoJSON source holding all routes as separate features, and
      // MapLibre paints features in array order, so whichever route
      // happens to come LAST in that array wins the overlap visually,
      // regardless of which one is actually selected. A route-highlight
      // source containing only the one highlighted route's geometry, in
      // its own layer added after every other route layer, guarantees it's
      // always on top — a white halo underneath it, wider than the line
      // itself, is what makes it pop even against a same-color-family
      // default route directly beneath it, not just thicker.
      map.addSource("route-highlight", { type: "geojson", data: emptyFeatureCollection() });
      map.addLayer({
        id: "routes-line-highlight-halo",
        type: "line",
        source: "route-highlight",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": "#ffffff", "line-width": ROUTE_WIDTH_HIGHLIGHTED + 4, "line-opacity": 0.9 },
      });
      map.addLayer({
        id: "routes-line-highlight",
        type: "line",
        source: "route-highlight",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": ROUTE_COLOR_HIGHLIGHTED, "line-width": ROUTE_WIDTH_HIGHLIGHTED },
      });

      map.addSource("checkpoints", { type: "geojson", data: emptyFeatureCollection() });
      map.addLayer({
        id: "checkpoints-circle",
        type: "circle",
        source: "checkpoints",
        paint: {
          // Visual weight follows severity — a severe/no-travel checkpoint
          // should read as "bigger problem", not just "different color",
          // at a glance and without needing to click into the popup.
          "circle-radius": [
            "match",
            ["get", "level"],
            "severe",
            9,
            "no_travel",
            10,
            "high",
            7.5,
            "moderate",
            6.5,
            6,
          ],
          "circle-color": [
            "match",
            ["get", "level"],
            "low",
            RISK_COLORS.low,
            "moderate",
            RISK_COLORS.moderate,
            "high",
            RISK_COLORS.high,
            "severe",
            RISK_COLORS.severe,
            "no_travel",
            RISK_COLORS.no_travel,
            "#888888",
          ],
          "circle-stroke-width": 1.5,
          "circle-stroke-color": "#ffffff",
        },
      });

      for (const [condition, emoji] of Object.entries(CONDITION_EMOJI)) {
        if (!map.hasImage(condition)) map.addImage(condition, emojiToImageData(emoji), { pixelRatio: 2 });
      }
      map.addSource("weather-glyphs", { type: "geojson", data: emptyFeatureCollection() });
      map.addLayer({
        id: "weather-glyphs",
        type: "symbol",
        source: "weather-glyphs",
        layout: {
          "icon-image": ["get", "condition"],
          "icon-size": ["interpolate", ["linear"], ["zoom"], 3, 0.3, 9, 0.55],
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
        },
      });

      for (const lineLayerId of ["routes-line", "routes-line-highlight"]) {
        map.on("click", lineLayerId, (event: MapLayerMouseEvent) => {
          if (useTripUIStore.getState().pickingField) return;
          const routeId = event.features?.[0]?.properties?.routeId as string | undefined;
          if (routeId) selectRoute(routeId);
        });
      }

      map.on("click", "checkpoints-circle", (event: MapLayerMouseEvent) => {
        if (useTripUIStore.getState().pickingField) return;
        const feature = event.features?.[0];
        if (!feature) return;
        const coordinates = (feature.geometry as Point).coordinates as [number, number];

        popupRef.current?.remove();
        popupRef.current = new Popup({ closeButton: true })
          .setLngLat(coordinates)
          .setHTML(checkpointPopupHtml(feature.properties as Record<string, unknown>))
          .addTo(map);
      });

      for (const layerId of ["routes-line", "routes-line-highlight", "checkpoints-circle"]) {
        map.on("mouseenter", layerId, () => {
          if (!useTripUIStore.getState().pickingField) map.getCanvas().style.cursor = "pointer";
        });
        map.on("mouseleave", layerId, () => {
          map.getCanvas().style.cursor = useTripUIStore.getState().pickingField ? "crosshair" : "";
        });
      }
    });

    return () => {
      // originMarkerRef/destinationMarkerRef/truckMarkersRef are created/
      // updated by other effects, not this one — reviewed false-positive on
      // the "ref may be stale" lint warning: unmount cleanup deliberately
      // wants whatever marker(s) exist *at unmount*, not a value captured
      // when this effect was set up (there is none yet at that point).
      popupRef.current?.remove();
      originMarkerRef.current?.remove();
      destinationMarkerRef.current?.remove();
      for (const marker of truckMarkersRef.current.values()) marker.remove();
      truckMarkersRef.current.clear();
      map.remove();
      mapRef.current = null;
    };
  }, [selectRoute]);

  // Crosshair cursor while a "pick on map" field is active.
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    map.getCanvas().style.cursor = pickingField ? "crosshair" : "";
  }, [pickingField]);

  // Heatmap gets its own effect, deliberately separate from the
  // routes/checkpoints one below: dragging the slider changes `hour`
  // dozens of times a second and must only touch the heatmap source, never
  // recompute or re-set route/checkpoint data it has nothing to do with.
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;

    const applyHeatmap = () => {
      const heatmapSource = map.getSource("heatmap") as GeoJSONSource | undefined;
      if (!heatmapSource) return;

      const showHeatmap = heatmapLayer != null;
      map.setLayoutProperty("corridor-heatmap-cloud", "visibility", showHeatmap ? "visible" : "none");
      if (heatmapLayer) {
        const style = HEATMAP_CLOUD_STYLES[heatmapLayer];
        map.setPaintProperty("corridor-heatmap-cloud", "heatmap-weight", style.weight as number);
        map.setPaintProperty("corridor-heatmap-cloud", "heatmap-color", style.color as ExpressionSpecification);
      }
      heatmapSource.setData(heatmapToFeatureCollection(heatmap, forecastHour, heatmapLayer));

      // Purely additive to routes-line, never a replacement for it — this
      // is the only thing this layer's visibility depends on, and nothing
      // here ever touches routes-line's own visibility/paint.
      map.setLayoutProperty("routes-risk-line", "visibility", heatmapLayer === "risk" ? "visible" : "none");
    };

    // Gate on whether OUR source exists yet, not map.isStyleLoaded(): that
    // also reflects whether every tile for the *current viewport* has
    // finished loading, and flips back to false after any pan/zoom (e.g.
    // the data effect's own fitBounds calls) — which then fell through to
    // map.once("load", ...), a ONE-TIME startup event that had already
    // fired and would never fire again, silently dropping the update.
    if (map.getSource("heatmap")) {
      applyHeatmap();
    } else {
      map.once("load", applyHeatmap);
    }
  }, [heatmap, forecastHour, heatmapLayer]);

  // Truck markers: one per route, repositioned along that route's own
  // polyline at the slider's forecast hour. Plain DOM Markers (not a
  // GeoJSON layer) since there are at most a handful of routes — no need
  // for the load-gate the heatmap/routes effects need, Markers don't
  // depend on style/tile load the way sources and layers do.
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;

    const liveIds = new Set(routes.map((r) => r.route_id));
    for (const [routeId, marker] of truckMarkersRef.current) {
      if (!liveIds.has(routeId)) {
        marker.remove();
        truckMarkersRef.current.delete(routeId);
      }
    }

    for (const route of routes) {
      const position = positionAtHour(route.polyline, route.duration_breakpoints, forecastHour);
      if (!position) continue;

      const highlighted = route.route_id === (selectedRouteId ?? recommendedRouteId);
      const existing = truckMarkersRef.current.get(route.route_id);

      if (existing) {
        updateTruckMarkerElement(existing.getElement(), position, highlighted);
        existing.setLngLat([position.lon, position.lat]);
      } else {
        const element = truckMarkerElement(route.route_id);
        updateTruckMarkerElement(element, position, highlighted);
        const marker = new Marker({ element, anchor: "center" }).setLngLat([position.lon, position.lat]).addTo(map);
        truckMarkersRef.current.set(route.route_id, marker);
      }
    }
  }, [routes, forecastHour, selectedRouteId, recommendedRouteId]);

  // Push new data / highlight styling / pick markers into the existing map
  // whenever the analysis result, selection, or picked locations change.
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;

    const applyData = () => {
      const routesSource = map.getSource("routes") as GeoJSONSource | undefined;
      const checkpointsSource = map.getSource("checkpoints") as GeoJSONSource | undefined;
      if (!routesSource || !checkpointsSource) return;

      routesSource.setData(routesToFeatureCollection(routes));
      checkpointsSource.setData(checkpointsToFeatureCollection(routes));
      (map.getSource("weather-glyphs") as GeoJSONSource | undefined)?.setData(
        weatherGlyphFeatureCollection(routes),
      );
      (map.getSource("route-risk") as GeoJSONSource | undefined)?.setData(routeRiskSegments(routes));

      const highlightedId = selectedRouteId ?? recommendedRouteId ?? "";
      map.setPaintProperty("routes-line", "line-color", [
        "case",
        ["==", ["get", "routeId"], highlightedId],
        ROUTE_COLOR_HIGHLIGHTED,
        ROUTE_COLOR_DEFAULT,
      ]);
      map.setPaintProperty("routes-line", "line-width", [
        "case",
        ["==", ["get", "routeId"], highlightedId],
        ROUTE_WIDTH_HIGHLIGHTED,
        ROUTE_WIDTH_DEFAULT,
      ]);
      // Recedes the non-selected routes so the highlighted one reads
      // clearly even before accounting for z-order at all — the
      // route-highlight layer below is what guarantees it visually wins
      // any exact overlap, but dimming the others helps at a glance even
      // where paths diverge.
      map.setPaintProperty("routes-line", "line-opacity", [
        "case",
        ["==", ["get", "routeId"], highlightedId],
        1,
        0.5,
      ]);
      // routes-line's own highlighted feature can still end up visually
      // underneath a non-highlighted one wherever their paths coincide —
      // "routes" is one source with all routes as separate features, and
      // MapLibre paints features in array order, not by paint-property
      // value, so whichever route happens to come LAST in that array wins
      // the overlap on screen regardless of which one is actually
      // selected. route-highlight is a second, tiny source holding only
      // the highlighted route's own geometry, in its own layer added after
      // every other route layer — guaranteed on top no matter the array
      // order, with a white halo underneath so it pops even against a
      // same-color-family route directly beneath it.
      const highlightedRoute = routes.find((route) => route.route_id === highlightedId);
      (map.getSource("route-highlight") as GeoJSONSource | undefined)?.setData(
        highlightedRoute
          ? {
              type: "FeatureCollection",
              features: [
                {
                  type: "Feature",
                  properties: { routeId: highlightedRoute.route_id },
                  geometry: {
                    type: "LineString",
                    coordinates: highlightedRoute.polyline.map(([lat, lon]) => [lon, lat]),
                  },
                },
              ],
            }
          : emptyFeatureCollection(),
      );
      // The ribbon only needs to stand out for the highlighted route — it's
      // a secondary accent, not the primary selection indicator, so it
      // stays thin for non-selected routes rather than matching
      // routes-line's own highlighted/default width split one-for-one.
      map.setPaintProperty("routes-risk-line", "line-width", [
        "case",
        ["==", ["get", "routeId"], highlightedId],
        5,
        2.5,
      ]);
      map.setPaintProperty("routes-risk-line", "line-opacity", [
        "case",
        ["==", ["get", "routeId"], highlightedId],
        0.9,
        0.45,
      ]);

      // Plain pin markers for picked locations before an analysis exists —
      // once routes come in, origin/destination are already the first/last
      // checkpoint, so the pins would just duplicate those markers.
      const showPins = routes.length === 0;
      syncMarker(originMarkerRef, showPins ? pickedOrigin : null, map, "#2e7d32");
      syncMarker(destinationMarkerRef, showPins ? pickedDestination : null, map, "#c62828");

      const extraPoints = showPins ? [pickedOrigin, pickedDestination].filter((p): p is Location => p != null) : [];
      const bounds = computeBounds(routes, extraPoints);
      if (bounds) {
        map.fitBounds(bounds, { padding: 64, duration: 300, maxZoom: 12 });
      }
    };

    // Gate on whether OUR source exists yet, not map.isStyleLoaded() — see
    // the heatmap effect above for why that check is wrong here: it also
    // reflects current-viewport tile loading, flips back to false after
    // this very function's own fitBounds call, and would then silently
    // never re-fire (map.once("load", ...) only fires the map's one-time
    // startup event, which has already happened by then).
    if (map.getSource("routes")) {
      applyData();
    } else {
      map.once("load", applyData);
    }
  }, [routes, selectedRouteId, recommendedRouteId, pickedOrigin, pickedDestination]);

  return <div ref={containerRef} className="map-view" />;
}

function syncMarker(
  ref: MutableRefObject<Marker | null>,
  location: Location | null,
  map: MapLibreMap,
  color: string,
) {
  if (!location) {
    ref.current?.remove();
    ref.current = null;
    return;
  }
  if (!ref.current) {
    ref.current = new Marker({ color }).setLngLat([location.lon, location.lat]).addTo(map);
  } else {
    ref.current.setLngLat([location.lon, location.lat]);
  }
}

// Top-down silhouette (cab + trailer + wheel marks), not a side-view
// glyph: a side-view truck can only ever flip left/right, which is a lie
// for any route that isn't running due east-west. This one points north
// (bearing 0) by default and rotates with a plain CSS transform straight
// from position.bearingDeg below — the same "rotate the vehicle glyph
// toward true heading" convention turn-by-turn nav apps use, not
// decoration for its own sake.
const TRUCK_ICON_SVG = `
  <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path d="M12 1.6 15.4 8.2 8.6 8.2Z" />
    <rect x="8.2" y="8.2" width="7.6" height="12.2" rx="1.4" />
    <rect x="6.1" y="9.6" width="1.5" height="3.1" rx="0.7" />
    <rect x="16.4" y="9.6" width="1.5" height="3.1" rx="0.7" />
    <rect x="6.1" y="15.3" width="1.5" height="3.1" rx="0.7" />
    <rect x="16.4" y="15.3" width="1.5" height="3.1" rx="0.7" />
  </svg>
`;

// Small corner badges reusing the trip's own three states
// (lib/routeGeometry.ts's RoutePosition["state"]) — en route is the
// default, unbadged look; waiting/arrived get a glance-able icon instead
// of only a hover tooltip, since those are the two states where "is this
// truck actually moving right now" is the thing worth knowing at a glance.
const STATUS_ICON_SVG: Record<"waiting" | "arrived", string> = {
  waiting: `
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3.2 2" />
    </svg>
  `,
  arrived: `
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M4 12l5 5L20 6" />
    </svg>
  `,
};

function truckMarkerElement(routeId: string): HTMLDivElement {
  const root = document.createElement("div");
  root.className = "truck-marker";
  root.innerHTML = `
    <span class="truck-marker__badge">
      <span class="truck-marker__icon">${TRUCK_ICON_SVG}</span>
      <span class="truck-marker__status"></span>
    </span>
    <span class="truck-marker__label">${routeId}</span>
  `;
  return root;
}

const STATE_LABEL: Record<RoutePosition["state"], string> = {
  waiting: "waiting to depart",
  enroute: "en route",
  arrived: "arrived",
};

function updateTruckMarkerElement(root: HTMLElement, position: RoutePosition, highlighted: boolean): void {
  root.classList.toggle("truck-marker--highlighted", highlighted);
  root.title = `${STATE_LABEL[position.state]}`;

  const icon = root.querySelector<HTMLElement>(".truck-marker__icon");
  if (icon) icon.style.transform = `rotate(${position.bearingDeg}deg)`;

  const status = root.querySelector<HTMLElement>(".truck-marker__status");
  if (status) {
    status.classList.toggle("truck-marker__status--waiting", position.state === "waiting");
    status.classList.toggle("truck-marker__status--arrived", position.state === "arrived");
    status.innerHTML = position.state === "enroute" ? "" : STATUS_ICON_SVG[position.state];
  }
}
