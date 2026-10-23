// The only module that knows the HTTP transport (base URL, headers, error
// shape). useGet/usePost call this instead of touching fetch() directly.

// "no_route_found": the provider confirmed no drivable route exists for
// these locations/profile. "provider_error": the provider itself failed
// (network, timeout, rate limit, malformed response) — not evidence the
// route is impossible. See backend/api/views.py for where these are set;
// keeping the distinction as a typed field (not a message string to
// pattern-match) is what lets the UI pick the right message/actions
// without being one wording change away from silently breaking.
export type ApiErrorCode = "no_route_found" | "provider_error" | null;

export class ApiError extends Error {
  status: number;
  errorCode: ApiErrorCode;

  constructor(status: number, message: string, errorCode: ApiErrorCode = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.errorCode = errorCode;
  }
}

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });

  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    const { message, errorCode } = extractErrorInfo(body);
    throw new ApiError(response.status, message ?? `Request failed with status ${response.status}`, errorCode);
  }

  return (await response.json()) as T;
}

// DRF returns either {"detail": "...", "error_code"?: "..."} (our own
// raised errors) or {"field_name": ["message", ...]} (serializer
// validation errors, which never carry error_code).
function extractErrorInfo(body: unknown): { message: string | null; errorCode: ApiErrorCode } {
  if (!body || typeof body !== "object") return { message: null, errorCode: null };
  const record = body as Record<string, unknown>;

  const errorCode: ApiErrorCode =
    record.error_code === "no_route_found" || record.error_code === "provider_error"
      ? record.error_code
      : null;

  if (typeof record.detail === "string") return { message: record.detail, errorCode };

  const [firstField] = Object.keys(record);
  const firstValue = firstField ? record[firstField] : undefined;
  if (Array.isArray(firstValue) && typeof firstValue[0] === "string") {
    return { message: `${firstField}: ${firstValue[0]}`, errorCode: null };
  }
  return { message: null, errorCode };
}

export function apiGet<T>(path: string): Promise<T> {
  return request<T>(path, { method: "GET" });
}

export function apiPost<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, { method: "POST", body: JSON.stringify(body) });
}
