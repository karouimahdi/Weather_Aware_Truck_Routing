import { useMutation, type UseMutationOptions } from "@tanstack/react-query";
import { apiPost } from "../api/client";

// Generic POST -> useMutation wrapper. A form-triggered action (submit,
// analyze, etc.) maps more naturally onto useMutation than useQuery: the
// caller decides when it fires (`.mutate(body)`), and gets back
// { mutate, data, error, isPending } without re-deriving that boilerplate
// at every call site.
export function usePost<TResponse, TBody>(
  path: string,
  options?: Omit<UseMutationOptions<TResponse, Error, TBody>, "mutationFn">,
) {
  return useMutation<TResponse, Error, TBody>({
    mutationFn: (body) => apiPost<TResponse>(path, body),
    ...options,
  });
}
