import { useMemo, useState, type FormEvent } from "react";
import {
  Alert,
  Button,
  CircularProgress,
  MenuItem,
  Stack,
  TextField,
} from "@mui/material";
import type { AnalyzeRequest, Location } from "../api/types";
import {
  DEPARTURE_MAX_FUTURE_DAYS,
  MAX_LOAD_WEIGHT_LB,
  fieldErrorsFrom,
  tripFormSchema,
  validateField,
  type TripFormFieldErrors,
} from "../lib/tripFormSchema";
import { LocationInput } from "./LocationInput";

const INTERVALS = [10, 25, 50] as const;

// <input type="datetime-local"> min/max are interpreted in the browser's
// own local time, with no timezone suffix — formatting "now" and "now +
// N days" this way is what makes the native picker itself refuse to open
// on an out-of-range date, before any JS validation even runs.
function toDatetimeLocalValue(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

interface TripFormProps {
  origin: Location | null;
  destination: Location | null;
  onOriginChange: (location: Location | null) => void;
  onDestinationChange: (location: Location | null) => void;
  onSubmit: (payload: AnalyzeRequest) => void;
  isPending: boolean;
  // Bump to remount just that field, clearing its internal draft text —
  // see the comment where App.tsx sets these for why this is needed.
  originResetKey?: number;
  destinationResetKey?: number;
}

export function TripForm({
  origin,
  destination,
  onOriginChange,
  onDestinationChange,
  onSubmit,
  isPending,
  originResetKey,
  destinationResetKey,
}: TripFormProps) {
  const [departureLocal, setDepartureLocal] = useState("");
  const [loadWeight, setLoadWeight] = useState("");
  const [interval, setInterval] = useState<(typeof INTERVALS)[number]>(25);
  const [fieldErrors, setFieldErrors] = useState<TripFormFieldErrors>({});

  // Computed once per mount (empty dep array), not on every render — the
  // picker's min/max only need to track "now" closely enough for a human
  // picking a date.
  const [departureMin, departureMax] = useMemo(() => {
    const now = new Date();
    return [
      toDatetimeLocalValue(now),
      toDatetimeLocalValue(new Date(now.getTime() + DEPARTURE_MAX_FUTURE_DAYS * 24 * 60 * 60 * 1000)),
    ];
  }, []);

  function handleSubmit(event: FormEvent) {
    event.preventDefault();

    const result = tripFormSchema.safeParse({ origin, destination, departureLocal, loadWeight, interval });
    const errors = fieldErrorsFrom(result);
    setFieldErrors(errors);
    if (!result.success) return;

    // <input type="datetime-local"> has no timezone; the Date constructor
    // treats that form as the browser's local time, so toISOString() gives
    // us the explicit-UTC-offset string the backend requires.
    onSubmit({
      origin: result.data.origin!,
      destination: result.data.destination!,
      departure_datetime: new Date(result.data.departureLocal).toISOString(),
      load_weight_lb: Number(result.data.loadWeight),
      sampling_interval_miles: result.data.interval,
    });
  }

  const hasFieldErrors = Object.keys(fieldErrors).length > 0;

  return (
    <Stack component="form" onSubmit={handleSubmit} spacing={2} noValidate>
      <LocationInput
        key={`origin-${originResetKey}`}
        id="origin"
        label="Origin"
        placeholder="Chicago, IL"
        field="origin"
        value={origin}
        onChange={(location) => {
          onOriginChange(location);
          if (fieldErrors.origin) setFieldErrors((prev) => ({ ...prev, origin: undefined }));
        }}
        error={fieldErrors.origin}
      />

      <LocationInput
        key={`destination-${destinationResetKey}`}
        id="destination"
        label="Destination"
        placeholder="Indianapolis, IN"
        field="destination"
        value={destination}
        onChange={(location) => {
          onDestinationChange(location);
          if (fieldErrors.destination) setFieldErrors((prev) => ({ ...prev, destination: undefined }));
        }}
        error={fieldErrors.destination}
      />

      <TextField
        id="departure"
        label="Departure"
        type="datetime-local"
        value={departureLocal}
        onChange={(e) => {
          setDepartureLocal(e.target.value);
          if (fieldErrors.departureLocal) setFieldErrors((prev) => ({ ...prev, departureLocal: undefined }));
        }}
        onBlur={(e) =>
          setFieldErrors((prev) => ({ ...prev, departureLocal: validateField("departureLocal", e.target.value) }))
        }
        slotProps={{ inputLabel: { shrink: true }, htmlInput: { min: departureMin, max: departureMax } }}
        error={Boolean(fieldErrors.departureLocal)}
        helperText={fieldErrors.departureLocal ?? `Within the next ${DEPARTURE_MAX_FUTURE_DAYS} days.`}
        fullWidth
      />

      <TextField
        id="load"
        label="Load weight (lb)"
        type="number"
        value={loadWeight}
        onChange={(e) => {
          setLoadWeight(e.target.value);
          if (fieldErrors.loadWeight) setFieldErrors((prev) => ({ ...prev, loadWeight: undefined }));
        }}
        onBlur={(e) =>
          setFieldErrors((prev) => ({ ...prev, loadWeight: validateField("loadWeight", e.target.value) }))
        }
        slotProps={{ htmlInput: { min: 0, max: MAX_LOAD_WEIGHT_LB, step: 100, inputMode: "numeric" } }}
        placeholder="20000"
        error={Boolean(fieldErrors.loadWeight)}
        helperText={fieldErrors.loadWeight}
        fullWidth
      />

      <TextField
        id="interval"
        label="Checkpoint interval"
        select
        value={interval}
        onChange={(e) => setInterval(Number(e.target.value) as (typeof INTERVALS)[number])}
        fullWidth
      >
        {INTERVALS.map((miles) => (
          <MenuItem key={miles} value={miles}>
            {miles} miles
          </MenuItem>
        ))}
      </TextField>

      {hasFieldErrors && (
        <Alert severity="error" variant="outlined">
          Fix the highlighted field{Object.keys(fieldErrors).length > 1 ? "s" : ""} above.
        </Alert>
      )}

      <Button
        type="submit"
        variant="contained"
        disabled={isPending}
        startIcon={isPending ? <CircularProgress color="inherit" size={16} /> : undefined}
        sx={{ minHeight: 44 }}
      >
        {isPending ? "Analyzing…" : "Analyze Routes"}
      </Button>
    </Stack>
  );
}
