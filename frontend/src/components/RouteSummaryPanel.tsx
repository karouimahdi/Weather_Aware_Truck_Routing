import { Box, Card, CardActionArea, CardContent, Chip, Stack, Typography, alpha } from "@mui/material";
import type { Recommendation, RouteResult } from "../api/types";
import { RISK_COLORS, RISK_LABELS, RISK_LEVELS } from "../lib/riskColors";
import { monoFontFamily } from "../theme";
import { useTripUIStore } from "../store/useTripUIStore";
import { CheckCircleIcon } from "./icons";

interface RouteSummaryPanelProps {
  routes: RouteResult[];
  recommendation: Recommendation;
}

function formatDuration(seconds: number): string {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.round((seconds % 3600) / 60);
  return `${hours}h ${minutes}m`;
}

export function RouteSummaryPanel({ routes, recommendation }: RouteSummaryPanelProps) {
  const selectedRouteId = useTripUIStore((state) => state.selectedRouteId);
  const selectRoute = useTripUIStore((state) => state.selectRoute);

  const sortedRoutes = [...routes].sort((a, b) => (a.rank ?? Infinity) - (b.rank ?? Infinity));

  return (
    <Stack spacing={1.5}>
      {sortedRoutes.map((route) => {
        const isSelected = (selectedRouteId ?? recommendation.winner_route_id) === route.route_id;
        const isWinner = recommendation.winner_route_id === route.route_id;

        return (
          <Card
            key={route.route_id}
            variant="outlined"
            sx={(theme) => ({
              borderColor: isSelected ? "primary.main" : "divider",
              borderWidth: isSelected ? 2 : 1,
              backgroundColor: isSelected ? alpha(theme.palette.primary.main, 0.06) : theme.palette.background.paper,
            })}
          >
            <CardActionArea onClick={() => selectRoute(route.route_id)} aria-pressed={isSelected} sx={{ minHeight: 44 }}>
              <CardContent sx={{ "&:last-child": { pb: 2 } }}>
                <Stack direction="row" sx={{ justifyContent: "space-between", alignItems: "center", mb: 1 }}>
                  <Stack direction="row" spacing={0.5} sx={{ alignItems: "center" }}>
                    {isWinner && <CheckCircleIcon size={14} />}
                    <Typography variant="subtitle2" color={isWinner ? "success.main" : "text.secondary"}>
                      {isWinner ? "Recommended" : route.rank ? `#${route.rank}` : "Unscored"}
                    </Typography>
                  </Stack>
                  <Typography variant="body2" sx={{ fontFamily: monoFontFamily }}>
                    {route.distance_mi.toFixed(0)} mi · {formatDuration(route.duration_s)}
                  </Typography>
                </Stack>

                <Box sx={{ display: "flex", flexWrap: "wrap", gap: 0.75 }}>
                  {RISK_LEVELS.filter((level) => route.risk_summary.miles_by_level[level] > 0).map((level) => (
                    <Chip
                      key={level}
                      size="small"
                      label={`${RISK_LABELS[level]}: ${route.risk_summary.miles_by_level[level].toFixed(0)} mi`}
                      sx={{ backgroundColor: RISK_COLORS[level], color: "#fff", fontWeight: 600 }}
                    />
                  ))}
                  {route.risk_summary.unassessed_miles > 0 && (
                    <Chip
                      size="small"
                      variant="outlined"
                      label={`Unknown: ${route.risk_summary.unassessed_miles.toFixed(0)} mi`}
                    />
                  )}
                </Box>

                {route.risk_summary.distance_weighted_avg_risk != null && (
                  <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
                    Avg risk: {route.risk_summary.distance_weighted_avg_risk.toFixed(2)}
                  </Typography>
                )}
              </CardContent>
            </CardActionArea>
          </Card>
        );
      })}
    </Stack>
  );
}
