import { useState } from "react";
import { Autocomplete, CircularProgress, IconButton, InputAdornment, TextField } from "@mui/material";
import type { Location } from "../api/types";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import { useGet } from "../hooks/useGet";
import { type PickingField, useTripUIStore } from "../store/useTripUIStore";
import { PinIcon } from "./icons";

const MIN_QUERY_LENGTH = 3;
// Nominatim's usage policy asks for well under 1 request/second; a longer
// debounce than a typical autocomplete (300ms) is the honest way to respect
// that without building a server-side rate limiter this app doesn't need.
const DEBOUNCE_MS = 800;
// Matches the backend's geocode cache TTL (api/views.py) — place labels
// don't change minute to minute, so re-typing a prefix you already
// searched reuses the cached result instead of refetching.
const SUGGESTIONS_STALE_TIME_MS = 5 * 60 * 1000;

interface LocationInputProps {
  id: string;
  label: string;
  placeholder?: string;
  field: Exclude<PickingField, null>;
  value: Location | null;
  onChange: (location: Location | null) => void;
  error?: string;
}

// MUI's Autocomplete manages its own open/close, keyboard nav, and
// outside-click dismissal — the hand-rolled versions of all three the
// previous plain-input version needed are gone, not just hidden behind
// the library.
export function LocationInput({ id, label, placeholder, field, value, onChange, error }: LocationInputProps) {
  // What the user is actively typing. Once `value` is set (a suggestion or
  // map click was picked), its label is shown instead — derived at render
  // time below via the Autocomplete's controlled `inputValue`, not synced
  // via an effect, so a pick from outside this component (the map) is
  // reflected immediately with no extra render.
  const [draftText, setDraftText] = useState("");

  const pickingField = useTripUIStore((state) => state.pickingField);
  const setPickingField = useTripUIStore((state) => state.setPickingField);
  const isPicking = pickingField === field;

  const debouncedQuery = useDebouncedValue(draftText, DEBOUNCE_MS);
  const shouldSearch = !value && debouncedQuery.trim().length >= MIN_QUERY_LENGTH;

  const { data: suggestions, isFetching } = useGet<Location[]>(
    ["geocode-suggest", debouncedQuery],
    `/api/geocode/suggest/?q=${encodeURIComponent(debouncedQuery)}`,
    { enabled: shouldSearch, staleTime: SUGGESTIONS_STALE_TIME_MS },
  );

  return (
    <Autocomplete<Location>
      id={id}
      options={suggestions ?? []}
      value={value}
      inputValue={value ? value.label : draftText}
      loading={isFetching}
      filterOptions={(options) => options} // server already filtered; don't re-filter client-side
      getOptionLabel={(option) => option.label}
      isOptionEqualToValue={(option, selected) => option.lat === selected.lat && option.lon === selected.lon}
      noOptionsText={debouncedQuery.trim().length < MIN_QUERY_LENGTH ? "Keep typing…" : "No matches"}
      onChange={(_event, selected) => onChange(selected)}
      onInputChange={(_event, newValue, reason) => {
        if (reason === "input") {
          setDraftText(newValue);
          onChange(null);
        } else if (reason === "clear") {
          setDraftText("");
          onChange(null);
        }
      }}
      renderInput={(params) => (
        <TextField
          {...params}
          label={label}
          placeholder={placeholder}
          error={Boolean(error)}
          helperText={error}
          slotProps={{
            ...params.slotProps,
            input: {
              ...params.slotProps.input,
              endAdornment: (
                <>
                  {isFetching && <CircularProgress color="inherit" size={16} />}
                  {params.slotProps.input.endAdornment}
                  <InputAdornment position="end">
                    <IconButton
                      size="small"
                      onClick={() => setPickingField(isPicking ? null : field)}
                      color={isPicking ? "primary" : "default"}
                      aria-label={isPicking ? "Cancel picking on map" : "Pick on map"}
                      aria-pressed={isPicking}
                      sx={{ minWidth: 44, minHeight: 44 }}
                    >
                      <PinIcon size={18} />
                    </IconButton>
                  </InputAdornment>
                </>
              ),
            },
          }}
        />
      )}
    />
  );
}
