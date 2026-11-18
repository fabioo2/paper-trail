import {
  type QueryClient,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { ApiError, apiClient, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { summaryKeys } from "@/features/home/api";
import { productKeys } from "@/features/products/api";
import { reviewKeys } from "@/features/reviews/api";
import { searchKeys } from "@/features/search/api";

type Schemas = components["schemas"];
export type ReceiptSummaryOut = Schemas["ReceiptSummaryOut"];
export type ReceiptDetailOut = Schemas["ReceiptDetailOut"];
export type LineItemOut = Schemas["LineItemOut"];
export type ReceiptStatus = Schemas["ReceiptStatus"];
export type ReceiptUploadOut = Schemas["ReceiptUploadOut"];
export type FailureReason = Schemas["FailureReason"];

export const RECEIPT_POLL_INTERVAL_MS = 2_000;
/** How long a receipt is followed while being read before the app stops polling it. */
export const READING_TIMEOUT_MS = 3 * 60_000;
/** Retries of one poll: three failures in a row stop polling. */
const RECEIPT_POLL_RETRIES = 2;

export const receiptKeys = {
  all: ["receipts"] as const,
  list: () => [...receiptKeys.all, "list"] as const,
  detail: (id: number) => [...receiptKeys.all, "detail", id] as const,
};

/** The receipts being read, each with the time it stops being followed. */
const watchedKey = ["watched-readings"] as const;

export interface WatchedReading {
  id: number;
  until: number;
}

/** Follow a receipt the worker is reading until it settles (`useWatchedReadings`), after an
 * upload, a Try again or a review answer; its cached detail takes the given status so an older
 * settled status doesn't end it early. */
export function watchReading(
  queryClient: QueryClient,
  receipt_id: number,
  { status, stage }: Pick<ReceiptDetailOut, "status" | "stage">,
) {
  if (isFinalStatus(status)) return;
  queryClient.setQueryData<ReceiptDetailOut>(
    receiptKeys.detail(receipt_id),
    (receipt) => receipt && { ...receipt, status, stage },
  );
  queryClient.setQueryData<WatchedReading[]>(watchedKey, (watched = []) => [
    ...watched.filter(({ id }) => id !== receipt_id),
    { id: receipt_id, until: Date.now() + READING_TIMEOUT_MS },
  ]);
}

/** The receipts `watchReading` follows. */
export function useWatchedReadings(): WatchedReading[] {
  return (
    useQuery({
      queryKey: watchedKey,
      queryFn: () => [] as WatchedReading[],
      staleTime: Infinity,
    }).data ?? []
  );
}

/** Stop following a receipt; `refetchAfterReading` when it settled (not when polling failed or
 * timed out). */
export function unwatchReading(
  queryClient: QueryClient,
  id: number,
  settled: boolean,
) {
  queryClient.setQueryData<WatchedReading[]>(watchedKey, (watched = []) =>
    watched.filter((reading) => reading.id !== id),
  );
  if (settled) void refetchAfterReading(queryClient);
}

/** Refetch everything a receipt that finished being read may have changed: the receipt list,
 * reviews, products, Home's summary and searches. */
export function refetchAfterReading(queryClient: QueryClient) {
  return Promise.all(
    [
      receiptKeys.list(),
      reviewKeys.all,
      productKeys.all,
      summaryKeys.all,
      searchKeys.all,
    ].map((queryKey) => queryClient.invalidateQueries({ queryKey })),
  );
}

/**
 * Receipts newest first, one page per cursor (`listReceipts`), polled while one is being read
 * (after a review answer, say). When the last one finishes, `refetchAfterReading`.
 */
export function useReceiptList() {
  const queryClient = useQueryClient();
  const list = useInfiniteQuery({
    queryKey: receiptKeys.list(),
    queryFn: ({ pageParam }) =>
      unwrap(
        apiClient.GET("/api/receipts", {
          params: { query: pageParam === null ? {} : { cursor: pageParam } },
        }),
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor,
    refetchInterval: (query) =>
      query.state.status !== "error" && anyProcessing(query.state.data?.pages)
        ? RECEIPT_POLL_INTERVAL_MS
        : false,
  });

  useRefetchWhenRead(list.data && anyProcessing(list.data.pages), queryClient);
  return list;
}

function anyProcessing(pages: Schemas["ReceiptPageOut"][] | undefined) {
  return (
    pages?.some((page) =>
      page.items.some((receipt) => receipt.status === "processing"),
    ) ?? false
  );
}

/**
 * One receipt with its lines and arithmetic report (`getReceipt`), polled while it is being
 * read (after Try again or a review answer, say) so its result shows without a refresh. When
 * it finishes, `refetchAfterReading`.
 */
export function useReceipt(id: number) {
  const queryClient = useQueryClient();
  const receipt = useQuery({
    queryKey: receiptKeys.detail(id),
    queryFn: () =>
      unwrap(
        apiClient.GET("/api/receipts/{receipt_id}", {
          params: { path: { receipt_id: id } },
        }),
      ),
    refetchInterval: (query) =>
      query.state.status !== "error" &&
      query.state.data?.status === "processing"
        ? RECEIPT_POLL_INTERVAL_MS
        : false,
  });

  const status = receipt.data?.status;
  useRefetchWhenRead(
    status === undefined ? undefined : status === "processing",
    queryClient,
  );
  return receipt;
}

function useRefetchWhenRead(
  processing: boolean | undefined,
  queryClient: QueryClient,
) {
  const wasProcessing = useRef(false);
  useEffect(() => {
    if (processing === undefined) return;
    if (wasProcessing.current && !processing) {
      void refetchAfterReading(queryClient);
    }
    wasProcessing.current = processing;
  }, [processing, queryClient]);
}

export interface UploadResult {
  receipt: ReceiptUploadOut;
  /** The server already had this photo and returned its receipt instead of reading it again. */
  duplicate: boolean;
}

/** Send one receipt photo (`uploadReceipt`): 202 queues a new receipt, 200 is a duplicate. */
export async function uploadReceipt(file: File): Promise<UploadResult> {
  const result = await apiClient.POST("/api/receipts", {
    // The generated type says string (OpenAPI binary); the serializer sends the File itself.
    body: { file: file as unknown as string },
    bodySerializer: () => {
      const form = new FormData();
      form.append("file", file);
      return form;
    },
  });
  const receipt = await unwrap(Promise.resolve(result));
  return { receipt, duplicate: result.response.status === 200 };
}

export function useUploadReceipt() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: uploadReceipt,
    onSuccess: ({ receipt }) => {
      watchReading(queryClient, receipt.id, receipt);
      return queryClient.invalidateQueries({ queryKey: receiptKeys.list() });
    },
  });
}

/**
 * Queue a FAILED receipt for the worker again (`retryReceipt`). On success the cached receipt
 * takes the answer's status at once; either way the receipt, the list, Home's summary (its
 * processing count) and searches are refetched: on a 409 the receipt may already be processing.
 */
export function useRetryReceipt(id: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () =>
      unwrap(
        apiClient.POST("/api/receipts/{receipt_id}/retry", {
          params: { path: { receipt_id: id } },
        }),
      ),
    onSuccess: ({ status, stage }) => {
      queryClient.setQueryData<ReceiptDetailOut>(
        receiptKeys.detail(id),
        (receipt) =>
          receipt && { ...receipt, status, stage, failure_reason: null },
      );
      watchReading(queryClient, id, { status, stage });
    },
    onSettled: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: receiptKeys.detail(id) }),
        queryClient.invalidateQueries({ queryKey: receiptKeys.list() }),
        queryClient.invalidateQueries({ queryKey: summaryKeys.all }),
        queryClient.invalidateQueries({ queryKey: searchKeys.all }),
      ]),
  });
}

export function isFinalStatus(status: ReceiptStatus): boolean {
  return status !== "processing";
}

/**
 * A just-uploaded receipt, polled until the worker settles it or `enabled` turns off. A failed
 * poll is retried on the poll's own beat; a client error (e.g. 404) or the third failure in a
 * row is an error, and polling stops until `refetch`.
 */
export function useReceiptProgress(id: number, enabled: boolean) {
  return useQuery({
    queryKey: receiptKeys.detail(id),
    queryFn: ({ signal }) =>
      unwrap(
        apiClient.GET("/api/receipts/{receipt_id}", {
          params: { path: { receipt_id: id } },
          signal,
        }),
      ),
    enabled,
    retry: (failureCount, error) =>
      !(error instanceof ApiError && error.status < 500) &&
      failureCount < RECEIPT_POLL_RETRIES,
    retryDelay: RECEIPT_POLL_INTERVAL_MS,
    // A second observer (the scan page and `ReadingWatcher`) must not re-poll a receipt that
    // already failed; Try again refetches it.
    retryOnMount: false,
    refetchInterval: (query) => {
      if (query.state.status === "error") return false;
      const status = query.state.data?.status;
      return status !== undefined && isFinalStatus(status)
        ? false
        : RECEIPT_POLL_INTERVAL_MS;
    },
  });
}
