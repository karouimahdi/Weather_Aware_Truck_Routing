import type { RiskLevel } from "../api/types";

export const RISK_LEVELS: RiskLevel[] = ["low", "moderate", "high", "severe", "no_travel"];

export const RISK_COLORS: Record<RiskLevel, string> = {
  low: "#2e7d32",
  moderate: "#f9a825",
  high: "#ef6c00",
  severe: "#c62828",
  no_travel: "#4a148c",
};

export const RISK_LABELS: Record<RiskLevel, string> = {
  low: "Low",
  moderate: "Moderate",
  high: "High",
  severe: "Severe",
  no_travel: "No Travel",
};
