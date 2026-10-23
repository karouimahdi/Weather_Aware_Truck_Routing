import { z } from "zod";

// Mirrors backend/api/serializers.py's RouteAnalysisRequestSerializer exactly
// (MAX_LOAD_WEIGHT_LB, DEPARTURE_MAX_FUTURE_DAYS) — client-side validation is
// a fast-fail UX layer, the server is still the real enforcement point, so
// these constants and the server's must stay in sync by hand.
export const MAX_LOAD_WEIGHT_LB = 80_000;
export const DEPARTURE_MAX_FUTURE_DAYS = 7;
// Looser than the server's 15-minute grace, deliberately: this only gates
// client-side feedback, erring permissive here just means "let the server
// have the final say" rather than confusingly blocking a borderline-valid
// submission before it even leaves the browser.
const DEPARTURE_PAST_GRACE_MS = 60_000;

const locationSchema = z
  .object({ label: z.string(), lat: z.number(), lon: z.number() })
  .nullable()
  .refine((value) => value !== null, {
    message: "Pick a location from the suggestions or the map.",
  });

// <input type="datetime-local"> has no timezone; the Date constructor
// treats that string as the browser's local time.
const departureLocalSchema = z
  .string()
  .min(1, "Departure date and time is required.")
  .refine((value) => !Number.isNaN(new Date(value).getTime()), {
    message: "Enter a valid departure date and time.",
  })
  .refine((value) => new Date(value).getTime() >= Date.now() - DEPARTURE_PAST_GRACE_MS, {
    message: "Departure can't be in the past.",
  })
  .refine(
    (value) => new Date(value).getTime() <= Date.now() + DEPARTURE_MAX_FUTURE_DAYS * 86_400_000,
    { message: `Departure must be within the next ${DEPARTURE_MAX_FUTURE_DAYS} days.` },
  );

const loadWeightSchema = z
  .string()
  .min(1, "Load weight is required.")
  .refine((value) => Number.isFinite(Number(value)), { message: "Enter a valid number." })
  .refine((value) => Number(value) >= 0, { message: "Load weight can't be negative." })
  .refine((value) => Number(value) <= MAX_LOAD_WEIGHT_LB, {
    message: `Load weight can't exceed ${MAX_LOAD_WEIGHT_LB.toLocaleString()} lb.`,
  });

export const tripFormSchema = z.object({
  origin: locationSchema,
  destination: locationSchema,
  departureLocal: departureLocalSchema,
  loadWeight: loadWeightSchema,
  interval: z.union([z.literal(10), z.literal(25), z.literal(50)]),
});

export type TripFormInput = z.input<typeof tripFormSchema>;
export type TripFormFieldErrors = Partial<Record<keyof TripFormInput, string>>;

/** First issue message per top-level field, keyed for direct use as TextField helperText. */
export function fieldErrorsFrom(
  result: ReturnType<typeof tripFormSchema.safeParse>,
): TripFormFieldErrors {
  if (result.success) return {};
  const errors: TripFormFieldErrors = {};
  for (const issue of result.error.issues) {
    const key = issue.path[0] as keyof TripFormInput | undefined;
    if (key && !errors[key]) errors[key] = issue.message;
  }
  return errors;
}

export function validateField<K extends keyof typeof tripFormSchema.shape>(
  key: K,
  value: unknown,
): string | undefined {
  const result = tripFormSchema.shape[key].safeParse(value);
  return result.success ? undefined : result.error.issues[0]?.message;
}
