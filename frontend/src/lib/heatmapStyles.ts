import type { HeatmapLayer, HeatmapPoint } from "../api/types";
import { RISK_COLORS } from "./riskColors";

const CALM = "#38bdf8";

export const HEATMAP_LAYERS: { value: HeatmapLayer; label: string; hint: string }[] = [
  { value: "risk", label: "Risk", hint: "Risk level for this load" },
  { value: "wind", label: "Wind", hint: "Sustained wind — 25 / 35 / 45 / 55 mph" },
  { value: "rain", label: "Rain", hint: "Rain rate — 0.10 / 0.25 / 0.50 / 1.00 in/hr" },
  { value: "snow", label: "Snow", hint: "Snow rate — 0.5 / 1 / 2 / 3 in/hr" },
];

// Visible legend swatches for whichever layer is active — the real
// thresholds (same numbers routing/risk.py and each layer's
// heatmap-weight expression below use), surfaced so they're not just a
// hover-only tooltip (the `hint` string above) nobody discovers.
export const LAYER_LEGEND_STOPS: Record<HeatmapLayer, { color: string; label: string }[]> = {
  risk: [
    { color: RISK_COLORS.low, label: "Low" },
    { color: RISK_COLORS.moderate, label: "Moderate" },
    { color: RISK_COLORS.high, label: "High" },
    { color: RISK_COLORS.severe, label: "Severe" },
    { color: RISK_COLORS.no_travel, label: "No travel" },
  ],
  wind: [
    { color: CALM, label: "<25 mph" },
    { color: RISK_COLORS.moderate, label: "25–35" },
    { color: RISK_COLORS.high, label: "35–45" },
    { color: RISK_COLORS.severe, label: "45–55" },
    { color: RISK_COLORS.no_travel, label: "55+ mph" },
  ],
  rain: [
    { color: CALM, label: "<0.10 in/hr" },
    { color: RISK_COLORS.moderate, label: "0.10–0.25" },
    { color: RISK_COLORS.high, label: "0.25–0.50" },
    { color: RISK_COLORS.severe, label: "0.50–1.00" },
    { color: RISK_COLORS.no_travel, label: ">1.00 in/hr" },
  ],
  snow: [
    { color: CALM, label: "<0.5 in/hr" },
    { color: RISK_COLORS.moderate, label: "0.5–1.0" },
    { color: RISK_COLORS.high, label: "1.0–2.0" },
    { color: RISK_COLORS.severe, label: "2.0–3.0" },
    { color: RISK_COLORS.no_travel, label: ">3.0 in/hr" },
  ],
};

export function valuesForLayer(point: HeatmapPoint, layer: HeatmapLayer): (number | null)[] {
  switch (layer) {
    case "risk":
      return point.risk_by_hour;
    case "wind":
      return point.wind_by_hour;
    case "rain":
      return point.rain_by_hour;
    case "snow":
      return point.snow_by_hour;
  }
}

// One shared density gradient for every layer's "cloud" — heatmap-color
// maps the BLENDED density at a pixel (nearby points' weights summed, not
// any single point's raw reading) to a color, so trying to re-encode each
// layer's exact mph/in-hr thresholds into density stops would be false
// precision: density isn't the raw value. The real thresholds still do
// real work, just one step earlier, in each layer's own heatmap-weight
// expression below — this gradient only has to look like a calm->severe
// cloud, consistently, across all four layers.
const CLOUD_DENSITY_GRADIENT = [
  "interpolate",
  ["linear"],
  ["heatmap-density"],
  0,
  "rgba(0,0,0,0)",
  0.2,
  CALM,
  0.4,
  RISK_COLORS.moderate,
  0.6,
  RISK_COLORS.high,
  0.8,
  RISK_COLORS.severe,
  1,
  RISK_COLORS.no_travel,
];

// heatmap-weight per layer: each point's own raw value, normalized against
// that layer's real thresholds (the same numbers routing/risk.py and
// LAYER_LEGEND_STOPS use) onto roughly a 0..1 scale — this is what keeps a
// "severe" reading contributing more to the cloud than a "low" one, even
// though the color gradient above is shared and generic.
export const HEATMAP_CLOUD_STYLES: Record<HeatmapLayer, { weight: unknown; color: unknown }> = {
  risk: {
    weight: ["interpolate", ["linear"], ["get", "v"], 0, 0, 4, 1],
    color: CLOUD_DENSITY_GRADIENT,
  },
  wind: {
    weight: ["interpolate", ["linear"], ["get", "v"], 0, 0, 55, 1],
    color: CLOUD_DENSITY_GRADIENT,
  },
  rain: {
    weight: ["interpolate", ["linear"], ["get", "v"], 0, 0, 1.0, 1],
    color: CLOUD_DENSITY_GRADIENT,
  },
  snow: {
    weight: ["interpolate", ["linear"], ["get", "v"], 0, 0, 3.0, 1],
    color: CLOUD_DENSITY_GRADIENT,
  },
};
