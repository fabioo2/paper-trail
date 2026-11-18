import { useQuery } from "@tanstack/react-query";

import { apiClient, unwrap } from "@/api/client";
import type { components } from "@/api/schema";

type Schemas = components["schemas"];
export type SearchOut = Schemas["SearchOut"];
export type ReceiptMatchOut = Schemas["ReceiptMatchOut"];

/** The server refuses shorter queries. */
export const SEARCH_MIN_CHARS = 2;

export const searchKeys = {
  all: ["search"] as const,
  query: (q: string) => [...searchKeys.all, q] as const,
};

/** Products and receipts matching `query` (`search`); idle until it is long enough. */
export function useSearchResults(query: string) {
  const q = query.trim();
  return useQuery({
    queryKey: searchKeys.query(q),
    queryFn: () =>
      unwrap(apiClient.GET("/api/search", { params: { query: { q } } })),
    enabled: q.length >= SEARCH_MIN_CHARS,
  });
}
