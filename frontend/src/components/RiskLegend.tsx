import { Box, Paper, Stack, Typography } from "@mui/material";
import { RISK_COLORS, RISK_LABELS, RISK_LEVELS } from "../lib/riskColors";

export function RiskLegend() {
  return (
    <Paper
      elevation={3}
      sx={{
        position: "absolute",
        left: { xs: 8, sm: 16 },
        bottom: { xs: 8, sm: 16 },
        px: 1.5,
        py: 1,
        borderRadius: 2,
        maxWidth: "calc(100vw - 16px)",
        overflowX: "auto",
      }}
    >
      <Stack direction="row" spacing={1.5}>
        {RISK_LEVELS.map((level) => (
          <Stack key={level} direction="row" spacing={0.5} sx={{ alignItems: "center", flexShrink: 0 }}>
            <Box
              sx={{
                width: 10,
                height: 10,
                borderRadius: "50%",
                backgroundColor: RISK_COLORS[level],
                flexShrink: 0,
              }}
            />
            <Typography variant="caption" sx={{ whiteSpace: "nowrap" }}>
              {RISK_LABELS[level]}
            </Typography>
          </Stack>
        ))}
      </Stack>
    </Paper>
  );
}
