import { useQuery } from "@tanstack/react-query";

import { apiClient, unwrap } from "@/api/client";

export const HEALTH_REFETCH_INTERVAL_MS = 30_000;

/** Liveness of the backend, polled so the connection indicator stays current. */
export function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: () => unwrap(apiClient.GET("/api/health")),
    refetchInterval: HEALTH_REFETCH_INTERVAL_MS,
  });
}
