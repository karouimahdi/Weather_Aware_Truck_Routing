import { Button, Dialog, DialogActions, DialogContent, DialogTitle, Stack, Typography } from "@mui/material";
import { AlertTriangleIcon } from "./icons";

interface NoRouteModalProps {
  message: string;
  onChangeDestination: () => void;
  onEditLocations: () => void;
  onDismiss: () => void;
}

// A confirmed "no route exists" (backend error_code "no_route_found") gets
// a modal with the specific actions a user can actually take — unlike a
// provider/service failure (error_code "provider_error"), which stays the
// existing inline form-error banner, because "try again" IS the right
// action there and a modal would just be a redundant extra click.
//
// Visibility is derived directly from the mutation's error state in App.tsx
// (no internal "shown" state, no effect-driven open/close), so it can't
// desync or reopen itself on an unrelated re-render — it's open exactly
// when analysis.isError with this error_code is true, full stop.
export function NoRouteModal({ message, onChangeDestination, onEditLocations, onDismiss }: NoRouteModalProps) {
  return (
    <Dialog open onClose={onDismiss} aria-labelledby="no-route-title" aria-describedby="no-route-message" fullWidth maxWidth="xs">
      <DialogTitle id="no-route-title">
        <Stack direction="row" spacing={1} sx={{ alignItems: "center" }}>
          <AlertTriangleIcon size={22} />
          <span>No drivable route found</span>
        </Stack>
      </DialogTitle>
      <DialogContent>
        <Typography id="no-route-message" sx={{ mb: 1 }}>
          We couldn't find a connected route for the selected locations and vehicle profile. A
          ferry crossing or another transport mode may be required.
        </Typography>
        <Typography variant="body2" color="text.secondary">
          {message}
        </Typography>
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2 }}>
        <Button onClick={onEditLocations} sx={{ minHeight: 44 }}>
          Edit locations
        </Button>
        <Button onClick={onChangeDestination} variant="contained" sx={{ minHeight: 44 }}>
          Change destination
        </Button>
      </DialogActions>
    </Dialog>
  );
}
