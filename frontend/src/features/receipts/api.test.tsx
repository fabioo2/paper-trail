import { QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse, type JsonBodyType } from "msw";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useMonthSummary } from "@/features/home/api";
import { useProductList } from "@/features/products/api";
import {
  RECEIPT_POLL_INTERVAL_MS,
  useReceipt,
  useReceiptList,
  useRetryReceipt,
} from "@/features/receipts/api";
import { useReviewList } from "@/features/reviews/api";
import { useSearchResults } from "@/features/search/api";
import {
  receiptDetail,
  receiptPage,
  receiptSummary,
} from "@/test/fixtures/receipts";
import { monthSummary, searchResults } from "@/test/fixtures/summary";
import { server } from "@/test/msw/server";
import { createTestQueryClient } from "@/test/render";

/** The receipt, the list, Home's summary and a search answer; returns the paths fetched. */
function serveReads(): string[] {
  const reads: string[] = [];
  let detailReads = 0;
  server.use(
    http.get("/api/summary", ({ request }) => {
      reads.push(new URL(request.url).pathname);
      return HttpResponse.json(monthSummary());
    }),
    http.get("/api/search", ({ request }) => {
      reads.push(new URL(request.url).pathname);
      return HttpResponse.json(searchResults());
    }),
    http.get("/api/receipts", ({ request }) => {
      reads.push(new URL(request.url).pathname);
      return HttpResponse.json(receiptPage([]));
    }),
    http.get("/api/receipts/:receiptId", ({ request }) => {
      reads.push(new URL(request.url).pathname);
      detailReads += 1;
      return HttpResponse.json(
        detailReads === 1
          ? receiptDetail({
              status: "failed",
              stage: "failed",
              failure_reason: "unknown",
            })
          : receiptDetail({ status: "processing", stage: "queued" }),
      );
    }),
  );
  return reads;
}

describe("useRetryReceipt", () => {
  it.each([
    { name: "accepted", status: 202 },
    { name: "refused", status: 409 },
  ])(
    "refetches the receipt, the list, the summary and searches when the retry is $name",
    async ({ status }) => {
      const reads = serveReads();
      server.use(
        http.post("/api/receipts/:receiptId/retry", () =>
          HttpResponse.json(
            { id: 7, status: "processing", stage: "queued" },
            { status },
          ),
        ),
      );
      const queryClient = createTestQueryClient();
      const { result } = renderHook(
        () => ({
          receipt: useReceipt(7),
          list: useReceiptList(),
          summary: useMonthSummary("2026-11"),
          search: useSearchResults("eggs"),
          retry: useRetryReceipt(7),
        }),
        {
          wrapper: ({ children }: { children: ReactNode }) => (
            <QueryClientProvider client={queryClient}>
              {children}
            </QueryClientProvider>
          ),
        },
      );
      await waitFor(() => {
        expect(result.current.receipt.data?.status).toBe("failed");
        expect(result.current.list.isSuccess).toBe(true);
        expect(result.current.summary.isSuccess).toBe(true);
        expect(result.current.search.isSuccess).toBe(true);
      });
      reads.length = 0;

      await act(async () => {
        await result.current.retry.mutateAsync().catch(() => undefined);
      });

      await waitFor(() => {
        expect([...reads].sort()).toEqual([
          "/api/receipts",
          "/api/receipts/7",
          "/api/search",
          "/api/summary",
        ]);
      });
      await waitFor(() => {
        expect(result.current.receipt.data).toMatchObject({
          status: "processing",
          stage: "queued",
          failure_reason: null,
        });
      });
    },
  );
});

describe("a receipt finishing being read", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it.each([
    { name: "its detail", useReceipts: () => useReceipt(7).isSuccess },
    { name: "the list", useReceipts: () => useReceiptList().isSuccess },
  ])(
    "is polled on $name, then receipts, reviews, products, the summary and searches are refetched",
    async ({ useReceipts }) => {
      let status: "processing" | "completed" = "processing";
      const reads: string[] = [];
      const read = (request: Request) => {
        reads.push(new URL(request.url).pathname);
      };
      server.use(
        http.get("/api/receipts/:receiptId", ({ request }) => {
          read(request);
          return HttpResponse.json(receiptDetail({ id: 7, status }));
        }),
        http.get("/api/receipts", ({ request }) => {
          read(request);
          return HttpResponse.json(
            receiptPage([receiptSummary({ id: 7, status })]),
          );
        }),
        http.get("/api/reviews", ({ request }) => {
          read(request);
          return HttpResponse.json([]);
        }),
        http.get("/api/products", ({ request }) => {
          read(request);
          return HttpResponse.json([]);
        }),
        http.get("/api/summary", ({ request }) => {
          read(request);
          return HttpResponse.json(monthSummary());
        }),
        http.get("/api/search", ({ request }) => {
          read(request);
          return HttpResponse.json(searchResults());
        }),
      );
      const queryClient = createTestQueryClient();
      const { result } = renderHook(
        () => ({
          receipts: useReceipts(),
          reviews: useReviewList().isSuccess,
          products: useProductList().isSuccess,
          summary: useMonthSummary("2026-11").isSuccess,
          search: useSearchResults("eggs").isSuccess,
        }),
        {
          wrapper: ({ children }: { children: ReactNode }) => (
            <QueryClientProvider client={queryClient}>
              {children}
            </QueryClientProvider>
          ),
        },
      );
      await waitFor(() => {
        expect(Object.values(result.current).every(Boolean)).toBe(true);
      });
      const receiptPath = reads[0] ?? "";

      reads.length = 0;
      await act(() => vi.advanceTimersByTimeAsync(RECEIPT_POLL_INTERVAL_MS));
      expect(reads).toEqual([receiptPath]);

      reads.length = 0;
      status = "completed";
      await act(() => vi.advanceTimersByTimeAsync(RECEIPT_POLL_INTERVAL_MS));

      await waitFor(() => {
        expect(new Set(reads)).toEqual(
          new Set([
            receiptPath,
            "/api/reviews",
            "/api/products",
            "/api/summary",
            "/api/search",
          ]),
        );
      });
      reads.length = 0;
      await act(() =>
        vi.advanceTimersByTimeAsync(RECEIPT_POLL_INTERVAL_MS * 3),
      );
      expect(reads).toEqual([]);
    },
  );
});

interface Polled {
  isSuccess: boolean;
  isError: boolean;
}

describe("polling a receipt being read", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it.each([
    { name: "its detail", useReceipts: (): Polled => useReceipt(7) },
    { name: "the list", useReceipts: (): Polled => useReceiptList() },
  ])("stops on $name once a poll fails", async ({ useReceipts }) => {
    let failing = false;
    const reads: string[] = [];
    const answer = (request: Request, body: JsonBodyType) => {
      reads.push(new URL(request.url).pathname);
      return failing
        ? HttpResponse.json(null, { status: 500 })
        : HttpResponse.json(body);
    };
    server.use(
      http.get("/api/receipts/:receiptId", ({ request }) =>
        answer(request, receiptDetail({ id: 7, status: "processing" })),
      ),
      http.get("/api/receipts", ({ request }) =>
        answer(
          request,
          receiptPage([receiptSummary({ id: 7, status: "processing" })]),
        ),
      ),
    );
    const queryClient = createTestQueryClient();
    const { result } = renderHook(useReceipts, {
      wrapper: ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={queryClient}>
          {children}
        </QueryClientProvider>
      ),
    });
    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    failing = true;
    await act(() => vi.advanceTimersByTimeAsync(RECEIPT_POLL_INTERVAL_MS));
    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
    reads.length = 0;
    await act(() => vi.advanceTimersByTimeAsync(RECEIPT_POLL_INTERVAL_MS * 3));

    expect(reads).toEqual([]);
  });
});
