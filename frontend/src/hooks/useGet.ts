import { useQuery, type UseQueryOptions } from "@tanstack/react-query";
import { apiGet } from "../api/client";

// Generic GET -> useQuery wrapper. Not called anywhere yet (this project's
// only endpoint is POST /routes/analyze/, see usePost/useAnalyzeRoutes) —
// kept here as the matching half of the fetch pattern for the next GET
// endpoint (e.g. a geocoding-suggestions lookup).
export function useGet<T>(
  queryKey: readonly unknown[],
  path: string,
  options?: Omit<UseQueryOptions<T, Error>, "queryKey" | "queryFn">,
) {
  return useQuery<T, Error>({
    queryKey,
    queryFn: () => apiGet<T>(path),
    ...options,
  });
}
