import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import { apiClient, unwrap } from "@/api/client";
import type { components } from "@/api/schema";

type Schemas = components["schemas"];
export type ReceiptSummaryOut = Schemas["ReceiptSummaryOut"];
export type ReceiptDetailOut = Schemas["ReceiptDetailOut"];
export type LineItemOut = Schemas["LineItemOut"];
export type ReceiptStatus = Schemas["ReceiptStatus"];

export const receiptKeys = {
  all: ["receipts"] as const,
  list: () => [...receiptKeys.all, "list"] as const,
  detail: (id: number) => [...receiptKeys.all, "detail", id] as const,
};

/** Receipts newest first, one page per cursor (`listReceipts`). */
export function useReceiptList() {
  return useInfiniteQuery({
    queryKey: receiptKeys.list(),
    queryFn: ({ pageParam }) =>
      unwrap(
        apiClient.GET("/api/receipts", {
          params: { query: pageParam === null ? {} : { cursor: pageParam } },
        }),
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor,
  });
}

/** One receipt with its lines and arithmetic report (`getReceipt`). */
export function useReceipt(id: number) {
  return useQuery({
    queryKey: receiptKeys.detail(id),
    queryFn: () =>
      unwrap(
        apiClient.GET("/api/receipts/{receipt_id}", {
          params: { path: { receipt_id: id } },
        }),
      ),
  });
}
