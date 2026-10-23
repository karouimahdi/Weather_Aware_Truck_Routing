import { createTheme } from "@mui/material/styles";

// Palette from ui-ux-pro-max's "Real-Time / Operations Landing" +
// "Minimalism & Swiss Style" recommendation for this product type
// (logistics/dashboard tool): tracking-blue primary, delivery-orange accent,
// high-contrast neutral surfaces. Deliberately separate from RISK_COLORS
// (lib/riskColors.ts) — those are semantic data colors for the 5 ordinal
// risk levels and must stay stable regardless of UI chrome theming; palette
// here is purely interface chrome (buttons, borders, surfaces).
export const theme = createTheme({
  palette: {
    mode: "light",
    primary: { main: "#2563EB", light: "#3B82F6", dark: "#1E40AF", contrastText: "#FFFFFF" },
    secondary: { main: "#EA580C", contrastText: "#FFFFFF" },
    error: { main: "#DC2626" },
    background: { default: "#F7F9FC", paper: "#FFFFFF" },
    divider: "#E2E8F0",
    text: { primary: "#0F172A", secondary: "#475569" },
  },
  shape: { borderRadius: 10 },
  typography: {
    fontFamily: '"Fira Sans", "Helvetica Neue", Arial, sans-serif',
    h1: { fontSize: "1.125rem", fontWeight: 700 },
    button: { fontWeight: 600, textTransform: "none" },
  },
  components: {
    MuiButton: {
      styleOverrides: {
        root: { borderRadius: 8 },
      },
    },
    MuiTextField: {
      defaultProps: { size: "small" },
    },
    MuiFormControl: {
      defaultProps: { size: "small" },
    },
    MuiPaper: {
      styleOverrides: {
        root: { backgroundImage: "none" },
      },
    },
  },
});

// Numeric/data fields (distances, durations, ETAs, coordinates, weights)
// use the monospace pairing so columns of numbers align — the skill's
// "dashboard, data, analytics" typography match for this product type.
export const monoFontFamily = '"Fira Code", "SFMono-Regular", Consolas, monospace';
