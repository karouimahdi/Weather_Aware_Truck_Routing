import { Alert, AlertTitle } from "@mui/material";
import type { Recommendation } from "../api/types";

const STATUS_COPY: Record<Recommendation["status"], { label: string; severity: "success" | "warning" | "info" }> = {
  safe: { label: "Safe route found", severity: "success" },
  best_available_unsafe: { label: "No fully safe route", severity: "warning" },
  insufficient_data: { label: "Not enough data to recommend", severity: "info" },
};

export function RecommendationBanner({ recommendation }: { recommendation: Recommendation }) {
  const copy = STATUS_COPY[recommendation.status];
  return (
    <Alert severity={copy.severity} variant="filled" role="status">
      <AlertTitle sx={{ mb: 0 }}>{copy.label}</AlertTitle>
      {recommendation.reason}
    </Alert>
  );
}
