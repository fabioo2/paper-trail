import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { apiClient, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { productKeys } from "@/features/products/api";
import { receiptKeys } from "@/features/receipts/api";
import { reviewKeys } from "@/features/reviews/api";
import { searchKeys } from "@/features/search/api";

type Schemas = components["schemas"];
export type MonthSummaryOut = Schemas["MonthSummaryOut"];
export type WeekSpendOut = Schemas["WeekSpendOut"];
export type StoreSpendOut = Schemas["StoreSpendOut"];

export const SUMMARY_POLL_INTERVAL_MS = 3_000;

export const summaryKeys = {
  all: ["summary"] as const,
  month: (month: string) => [...summaryKeys.all, month] as const,
};

/** `YYYY-MM` of the date in the browser's own time zone. */
export function monthOf(date: Date): string {
  return `${String(date.getFullYear())}-${String(date.getMonth() + 1).padStart(2, "0")}`;
}

/**
 * The month's summary (`getSummary`), polled while a receipt is being read. When the last one
 * finishes, the receipts, products, reviews and searches it may have changed are refetched.
 */
export function useMonthSummary(month: string) {
  const queryClient = useQueryClient();
  const summary = useQuery({
    queryKey: summaryKeys.month(month),
    queryFn: () =>
      unwrap(apiClient.GET("/api/summary", { params: { query: { month } } })),
    // Fresh on every visit: a scan, review or edit elsewhere changes what Home shows.
    staleTime: 0,
    refetchInterval: (query) =>
      (query.state.data?.processing ?? 0) > 0
        ? SUMMARY_POLL_INTERVAL_MS
        : false,
  });

  const processing = summary.data?.processing;
  const wasProcessing = useRef(false);
  useEffect(() => {
    if (processing === undefined) return;
    if (wasProcessing.current && processing === 0) {
      for (const queryKey of [
        receiptKeys.all,
        productKeys.all,
        reviewKeys.all,
        searchKeys.all,
      ])
        void queryClient.invalidateQueries({ queryKey });
    }
    wasProcessing.current = processing > 0;
  }, [processing, queryClient]);

  return summary;
}
