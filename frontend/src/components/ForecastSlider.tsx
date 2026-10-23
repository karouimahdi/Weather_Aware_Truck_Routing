import { useEffect, useState, type ComponentType } from "react";
import {
  Box,
  IconButton,
  Paper,
  Slider,
  Stack,
  ToggleButton,
  ToggleButtonGroup,
  Typography,
} from "@mui/material";
import type { HeatmapLayer } from "../api/types";
import { HEATMAP_LAYERS, LAYER_LEGEND_STOPS } from "../lib/heatmapStyles";
import { useTripUIStore } from "../store/useTripUIStore";
import { CloudRainIcon, GaugeIcon, PauseIcon, PlayIcon, SnowflakeIcon, WindIcon, XIcon } from "./icons";

const PLAYBACK_STEP_MS = 500;

const LAYER_ICONS: Record<HeatmapLayer, ComponentType<{ size?: number }>> = {
  risk: GaugeIcon,
  wind: WindIcon,
  rain: CloudRainIcon,
  snow: SnowflakeIcon,
};

interface ForecastSliderProps {
  startUtc: string;
  maxHour: number;
}

export function ForecastSlider({ startUtc, maxHour }: ForecastSliderProps) {
  const forecastHour = useTripUIStore((state) => state.forecastHour);
  const setForecastHour = useTripUIStore((state) => state.setForecastHour);
  const heatmapLayer = useTripUIStore((state) => state.heatmapLayer);
  const setHeatmapLayer = useTripUIStore((state) => state.setHeatmapLayer);
  const [isPlaying, setIsPlaying] = useState(false);

  // Auto-scrub through the forecast so a trend (a storm moving in, wind
  // easing off) is visible without manually dragging the slider one hour
  // at a time. Wraps back to 0 rather than stopping at the end.
  useEffect(() => {
    if (!isPlaying) return;
    const id = window.setInterval(() => {
      useTripUIStore.setState((state) => ({
        forecastHour: state.forecastHour >= maxHour ? 0 : state.forecastHour + 1,
      }));
    }, PLAYBACK_STEP_MS);
    return () => window.clearInterval(id);
  }, [isPlaying, maxHour]);

  const selectedTime = new Date(new Date(startUtc).getTime() + forecastHour * 3_600_000);
  const timeLabel = selectedTime.toLocaleString(undefined, {
    weekday: "short",
    hour: "numeric",
    minute: "2-digit",
  });

  return (
    <Paper
      elevation={4}
      sx={{
        position: "absolute",
        top: { xs: 8, sm: 16 },
        left: "50%",
        transform: "translateX(-50%)",
        width: "min(480px, calc(100vw - 16px))",
        px: 2,
        py: 1.5,
        borderRadius: 2,
      }}
    >
      <Stack direction="row" sx={{ justifyContent: "space-between", alignItems: "baseline", mb: 1 }}>
        <Typography variant="subtitle2">Corridor forecast</Typography>
        <Typography variant="caption" color="text.secondary" sx={{ fontVariantNumeric: "tabular-nums" }}>
          {forecastHour === 0 ? "At departure" : `+${forecastHour}h`} · {timeLabel}
        </Typography>
      </Stack>

      <ToggleButtonGroup
        value={heatmapLayer ?? ""}
        exclusive
        onChange={(_event, next: HeatmapLayer | "" | null) => setHeatmapLayer(next ? next : null)}
        size="small"
        aria-label="Heatmap layer"
        sx={{ mb: 1, flexWrap: "wrap", gap: 0.5, "& .MuiToggleButtonGroup-grouped": { borderRadius: "999px !important", border: "1px solid" } }}
      >
        {HEATMAP_LAYERS.map(({ value, label, hint }) => {
          const Icon = LAYER_ICONS[value];
          return (
            <ToggleButton key={value} value={value} title={hint} sx={{ gap: 0.5, px: 1.25, minHeight: 32 }}>
              <Icon size={13} />
              {label}
            </ToggleButton>
          );
        })}
        <ToggleButton value="" title="Hide the heatmap" sx={{ gap: 0.5, px: 1.25, minHeight: 32 }}>
          <XIcon size={13} />
          Off
        </ToggleButton>
      </ToggleButtonGroup>

      {heatmapLayer && (
        <Stack
          direction="row"
          sx={{
            flexWrap: "wrap",
            gap: 1.5,
            fontSize: 11,
            color: "text.secondary",
            mb: 1,
            pb: 1,
            borderBottom: "1px solid",
            borderColor: "divider",
          }}
          role="list"
          aria-label={`${heatmapLayer} legend`}
        >
          {LAYER_LEGEND_STOPS[heatmapLayer].map(({ color, label }) => (
            <Stack key={label} direction="row" spacing={0.5} sx={{ alignItems: "center" }} role="listitem">
              <Box sx={{ width: 9, height: 9, borderRadius: "50%", backgroundColor: color, flexShrink: 0 }} />
              <span>{label}</span>
            </Stack>
          ))}
        </Stack>
      )}

      <Stack direction="row" spacing={1.5} sx={{ alignItems: "center" }}>
        <IconButton
          onClick={() => setIsPlaying((playing) => !playing)}
          color={isPlaying ? "primary" : "default"}
          aria-label={isPlaying ? "Pause forecast playback" : "Play forecast playback"}
          aria-pressed={isPlaying}
          sx={{ minWidth: 44, minHeight: 44, border: "1px solid", borderColor: "divider" }}
        >
          {isPlaying ? <PauseIcon size={14} /> : <PlayIcon size={14} />}
        </IconButton>

        <Box sx={{ flex: 1 }}>
          <Slider
            value={forecastHour}
            min={0}
            max={maxHour}
            step={1}
            onChange={(_event, value) => {
              setIsPlaying(false);
              setForecastHour(value as number);
            }}
            aria-label="Forecast hour offset from departure"
            size="small"
          />
          <Stack
            direction="row"
            sx={{ justifyContent: "space-between", fontSize: 11, color: "text.secondary", fontVariantNumeric: "tabular-nums" }}
          >
            <span>0h</span>
            <span>{Math.round(maxHour / 2)}h</span>
            <span>{maxHour}h</span>
          </Stack>
        </Box>
      </Stack>

      <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
        Blended from sampled points along the corridor — color shows nearby conditions, not a
        precise reading at any single spot.
      </Typography>
    </Paper>
  );
}
