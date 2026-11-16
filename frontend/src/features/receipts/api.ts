import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { ApiError, apiClient, unwrap } from "@/api/client";
import type { components } from "@/api/schema";

type Schemas = components["schemas"];
export type ReceiptSummaryOut = Schemas["ReceiptSummaryOut"];
export type ReceiptDetailOut = Schemas["ReceiptDetailOut"];
export type LineItemOut = Schemas["LineItemOut"];
export type ReceiptStatus = Schemas["ReceiptStatus"];
export type ReceiptUploadOut = Schemas["ReceiptUploadOut"];

export const RECEIPT_POLL_INTERVAL_MS = 2_000;
/** Retries of one poll: three failures in a row stop polling. */
const RECEIPT_POLL_RETRIES = 2;

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
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: receiptKeys.list() }),
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
    refetchInterval: (query) => {
      if (query.state.status === "error") return false;
      const status = query.state.data?.status;
      return status !== undefined && isFinalStatus(status)
        ? false
        : RECEIPT_POLL_INTERVAL_MS;
    },
  });
}
