import { useState } from "react";
import { Alert, Box, Paper, Skeleton, Stack, Typography } from "@mui/material";
import { apiGet, ApiError } from "./api/client";
import type { AnalyzeRequest, Location } from "./api/types";
import { ForecastSlider } from "./components/ForecastSlider";
import { RouteIcon } from "./components/icons";
import { MapView } from "./components/MapView";
import { NoRouteModal } from "./components/NoRouteModal";
import { RecommendationBanner } from "./components/RecommendationBanner";
import { RiskLegend } from "./components/RiskLegend";
import { RouteSummaryPanel } from "./components/RouteSummaryPanel";
import { TripForm } from "./components/TripForm";
import { useAnalyzeRoutes } from "./hooks/useAnalyzeRoutes";
import { useTripUIStore } from "./store/useTripUIStore";

const SIDEBAR_WIDTH = 400;

function App() {
  const analysis = useAnalyzeRoutes();
  const [origin, setOrigin] = useState<Location | null>(null);
  const [destination, setDestination] = useState<Location | null>(null);
  const [pickError, setPickError] = useState<string | null>(null);
  // Bumped whenever a location is cleared from outside the input itself
  // (the no-route modal's actions) — remounts just that LocationInput so
  // its internal leftover draft text resets along with the cleared value,
  // rather than the input displaying stale text for a location that's no
  // longer actually selected. See LocationInput's own displayText comment:
  // it only derives from `value` when `value` is set, so an external null
  // needs a remount, not a synced effect, to actually clear what's shown.
  const [originResetKey, setOriginResetKey] = useState(0);
  const [destinationResetKey, setDestinationResetKey] = useState(0);

  function handleSubmit(payload: AnalyzeRequest) {
    // A slider position left over from a previous, unrelated trip would be
    // confusing to land on — every new analysis starts back at departure.
    useTripUIStore.getState().setForecastHour(0);
    analysis.mutate(payload);
  }

  // Reverse-geocoding a map click is a one-shot imperative lookup, not a
  // reusable cached query — calling the transport directly here is more
  // honest than forcing it through useGet's declarative shape.
  const noRouteError =
    analysis.error instanceof ApiError && analysis.error.errorCode === "no_route_found"
      ? analysis.error
      : null;
  // A provider/service failure (error_code "provider_error", or any error
  // without a recognized code) stays the existing inline banner below —
  // only a *confirmed* no-route gets the modal with its own actions.
  const inlineError = analysis.isError && !noRouteError ? analysis.error : null;

  async function handleMapPick(coordinate: { lat: number; lon: number }) {
    const field = useTripUIStore.getState().pickingField;
    if (!field) return;

    setPickError(null);
    try {
      const location = await apiGet<Location>(
        `/api/geocode/reverse/?lat=${coordinate.lat}&lon=${coordinate.lon}`,
      );
      if (field === "origin") setOrigin(location);
      else setDestination(location);
    } catch {
      setPickError("Couldn't look up that location — try again or use the search box.");
    }
  }

  return (
    <Box sx={{ display: "flex", flexDirection: { xs: "column", md: "row" }, height: "100vh", width: "100%" }}>
      <Box
        component="aside"
        sx={{
          width: { xs: "100%", md: SIDEBAR_WIDTH },
          flexShrink: 0,
          maxHeight: { xs: "60vh", md: "100vh" },
          overflowY: "auto",
          p: { xs: 2, sm: 3 },
          borderRight: { md: "1px solid" },
          borderBottom: { xs: "1px solid", md: "none" },
          borderColor: "divider",
          backgroundColor: "background.paper",
        }}
      >
        <Stack direction="row" spacing={1} sx={{ alignItems: "center", mb: 0.5 }}>
          <RouteIcon size={20} />
          <Typography variant="h1" component="h1">
            Weather-Aware Truck Routing
          </Typography>
        </Stack>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Compare route options against the hourly forecast along each corridor.
        </Typography>

        <TripForm
          origin={origin}
          destination={destination}
          onOriginChange={setOrigin}
          onDestinationChange={setDestination}
          onSubmit={handleSubmit}
          isPending={analysis.isPending}
          originResetKey={originResetKey}
          destinationResetKey={destinationResetKey}
        />

        {pickError && (
          <Alert severity="error" sx={{ mt: 2 }} role="alert">
            {pickError}
          </Alert>
        )}

        {inlineError && (
          <Alert severity="error" sx={{ mt: 2 }} role="alert">
            {inlineError.message}
          </Alert>
        )}

        {analysis.isPending && (
          <Stack spacing={1.5} sx={{ mt: 2 }} aria-hidden="true">
            <Skeleton variant="rounded" height={90} />
            <Skeleton variant="rounded" height={90} />
          </Stack>
        )}

        {analysis.data && (
          <Stack spacing={1.5} sx={{ mt: 2 }}>
            <RecommendationBanner recommendation={analysis.data.recommendation} />
            {analysis.data.warnings.map((warning) => (
              <Alert key={warning} severity="warning">
                {warning}
              </Alert>
            ))}
            <RouteSummaryPanel routes={analysis.data.routes} recommendation={analysis.data.recommendation} />
          </Stack>
        )}
      </Box>

      {noRouteError && (
        <NoRouteModal
          message={noRouteError.message}
          onChangeDestination={() => {
            setDestination(null);
            setDestinationResetKey((key) => key + 1);
            analysis.reset();
          }}
          onEditLocations={() => {
            setOrigin(null);
            setDestination(null);
            setOriginResetKey((key) => key + 1);
            setDestinationResetKey((key) => key + 1);
            analysis.reset();
          }}
          onDismiss={() => analysis.reset()}
        />
      )}

      <Box component="main" sx={{ position: "relative", flex: 1, minHeight: { xs: "40vh", md: "auto" } }}>
        <MapView
          routes={analysis.data?.routes ?? []}
          recommendedRouteId={analysis.data?.recommendation.winner_route_id ?? null}
          pickedOrigin={origin}
          pickedDestination={destination}
          onMapPick={handleMapPick}
          heatmap={analysis.data?.heatmap ?? null}
        />

        {!analysis.data && !analysis.isPending && (
          <Box
            sx={{
              position: "absolute",
              inset: 0,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              pointerEvents: "none",
              zIndex: 1,
              px: 2,
            }}
          >
            <Paper elevation={4} sx={{ maxWidth: 280, p: 3, textAlign: "center" }}>
              <RouteIcon size={28} />
              <Typography variant="subtitle1" sx={{ mt: 1 }}>
                Enter a trip to see routes
              </Typography>
              <Typography variant="body2" color="text.secondary">
                Search an origin and destination, or pin them directly on the map.
              </Typography>
            </Paper>
          </Box>
        )}

        <RiskLegend />
        {analysis.data && (
          <ForecastSlider
            startUtc={analysis.data.heatmap.start_utc}
            maxHour={analysis.data.heatmap.hour_offsets.at(-1) ?? 0}
          />
        )}
      </Box>
    </Box>
  );
}

export default App;
