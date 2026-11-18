import { QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import {
  useReceipt,
  useReceiptList,
  useRetryReceipt,
} from "@/features/receipts/api";
import { receiptDetail, receiptPage } from "@/test/fixtures/receipts";
import { server } from "@/test/msw/server";
import { createTestQueryClient } from "@/test/render";

/** GET /api/receipts and /api/receipts/7 answer as given; returns the paths fetched. */
function serveReads(): string[] {
  const reads: string[] = [];
  let detailReads = 0;
  server.use(
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
    "refetches the shown receipt and list when the retry is $name",
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
      });
      reads.length = 0;

      await act(async () => {
        await result.current.retry.mutateAsync().catch(() => undefined);
      });

      expect([...reads].sort()).toEqual(["/api/receipts", "/api/receipts/7"]);
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
