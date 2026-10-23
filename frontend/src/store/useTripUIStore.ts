import { create } from "zustand";
import type { HeatmapLayer } from "../api/types";

export type PickingField = "origin" | "destination" | null;

// Client-only UI state shared across sibling components: which route is
// highlighted (MapView + RouteSummaryPanel), which location field a map
// click should fill in right now (LocationInput's "pick on map" button +
// MapView's click handler), and which heatmap hour is selected
// (ForecastSlider + MapView's heatmap layer). Deliberately NOT the
// analysis result itself — that's server data and stays in TanStack
// Query's cache via useAnalyzeRoutes, so there's one source of truth
// for it. forecastHour is exactly the case this store was set up for:
// moving the slider must never trigger a refetch, just restyle an
// already-mounted layer from data that's already in memory.
interface TripUIState {
  selectedRouteId: string | null;
  selectRoute: (routeId: string | null) => void;
  pickingField: PickingField;
  setPickingField: (field: PickingField) => void;
  forecastHour: number;
  setForecastHour: (hour: number) => void;
  // null = heatmap hidden. Defaults to "risk" so existing behavior is
  // unchanged until a user actually opens the layer toggle.
  heatmapLayer: HeatmapLayer | null;
  setHeatmapLayer: (layer: HeatmapLayer | null) => void;
}

export const useTripUIStore = create<TripUIState>((set) => ({
  selectedRouteId: null,
  selectRoute: (routeId) => set({ selectedRouteId: routeId }),
  pickingField: null,
  setPickingField: (field) => set({ pickingField: field }),
  forecastHour: 0,
  setForecastHour: (hour) => set({ forecastHour: hour }),
  heatmapLayer: "risk",
  setHeatmapLayer: (layer) => set({ heatmapLayer: layer }),
}));
