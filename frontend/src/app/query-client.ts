import { QueryClient } from "@tanstack/react-query";

import { ApiError } from "@/api/client";

const MAX_RETRIES = 2;

/** Client errors (4xx) will not fix themselves; network failures and 5xx get two retries. */
function shouldRetry(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status < 500) return false;
  return failureCount < MAX_RETRIES;
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        retry: shouldRetry,
      },
    },
  });
}
