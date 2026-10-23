// Mirrors backend/api/serializers.py exactly. Keep these two in sync by hand —
// there's one producer (the Django view) and one consumer (this app).

export type RiskLevel = "low" | "moderate" | "high" | "severe" | "no_travel";
export type HazardReason = "wind" | "rain" | "snow";

// A location the user explicitly resolved to coordinates — by picking an
// autocomplete suggestion or clicking the map — never free text the
// backend would geocode blind (see LocationInput.tsx for why).
export interface Location {
  label: string;
  lat: number;
  lon: number;
}

export interface AnalyzeRequest {
  origin: Location;
  destination: Location;
  departure_datetime: string; // ISO 8601 with explicit UTC offset or "Z"
  load_weight_lb: number;
  sampling_interval_miles: 10 | 25 | 50;
}

export interface WeatherObservation {
  wind_mph: number;
  rain_in_hr: number;
  snow_in_hr: number;
}

export interface CheckpointRisk {
  level: RiskLevel;
  wind_level: RiskLevel;
  rain_level: RiskLevel;
  snow_level: RiskLevel;
  wind_level_after_load_rule: RiskLevel;
  reasons: HazardReason[];
  prohibited: boolean;
}

export interface Checkpoint {
  sequence: number;
  distance_mi: number;
  lat: number;
  lon: number;
  eta_utc: string;
  is_origin: boolean;
  is_destination: boolean;
  weather: WeatherObservation | null;
  risk: CheckpointRisk | null; // null when the forecast horizon didn't cover this ETA
}

export interface MilesByLevel {
  low: number;
  moderate: number;
  high: number;
  severe: number;
  no_travel: number;
}

export interface RouteRiskSummary {
  miles_by_level: MilesByLevel;
  unassessed_miles: number;
  distance_weighted_avg_risk: number | null;
  has_no_travel: boolean;
  worst_checkpoint_sequence: number | null;
}

export interface DurationBreakpoint {
  distance_mi: number;
  cumulative_duration_s: number;
}

export interface RouteResult {
  route_id: string;
  distance_mi: number;
  duration_s: number;
  polyline: [number, number][]; // [lat, lon], route order
  // (distance_mi, cumulative_duration_s) aligned 1:1 with polyline vertices —
  // lets the truck marker interpolate position at any elapsed time, not
  // just at a checkpoint.
  duration_breakpoints: DurationBreakpoint[];
  checkpoints: Checkpoint[];
  risk_summary: RouteRiskSummary;
  rank: number | null; // 1 = best; null if this route couldn't be scored at all
}

export type RecommendationStatus = "safe" | "best_available_unsafe" | "insufficient_data";

export interface Recommendation {
  status: RecommendationStatus;
  winner_route_id: string | null;
  reason: string;
}

// risk_by_hour[i] is an ordinal 0-4 (0=low ... 4=no_travel) aligned with
// hour_offsets[i], or null if that hour fell outside the forecast horizon.
// Ordinal, not the string RiskLevel, to feed a map paint expression
// directly without a lookup per point per frame.
export interface HeatmapPoint {
  lat: number;
  lon: number;
  risk_by_hour: (number | null)[];
  wind_by_hour: (number | null)[]; // mph
  rain_by_hour: (number | null)[]; // in/hr
  snow_by_hour: (number | null)[]; // in/hr
}

export type HeatmapLayer = "risk" | "wind" | "rain" | "snow";

export interface Heatmap {
  start_utc: string; // hour_offset 0 = the trip's departure time, not request time
  hour_offsets: number[]; // 0..48, fixed for the whole response
  points: HeatmapPoint[];
}

export interface AnalyzeResponse {
  routes: RouteResult[];
  recommendation: Recommendation;
  heatmap: Heatmap;
  warnings: string[];
}
