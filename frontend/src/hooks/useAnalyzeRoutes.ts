import { usePost } from "./usePost";
import type { AnalyzeRequest, AnalyzeResponse } from "../api/types";

export function useAnalyzeRoutes() {
  return usePost<AnalyzeResponse, AnalyzeRequest>("/api/routes/analyze/");
}
