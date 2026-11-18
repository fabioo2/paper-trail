import { QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { receiptKeys, useRetryReceipt } from "@/features/receipts/api";
import { receiptDetail } from "@/test/fixtures/receipts";
import { server } from "@/test/msw/server";
import { createTestQueryClient } from "@/test/render";

describe("useRetryReceipt", () => {
  it.each([
    { name: "accepted", status: 202 },
    { name: "refused", status: 409 },
  ])(
    "refetches the receipt and the list when the retry is $name",
    async ({ status }) => {
      server.use(
        http.post("/api/receipts/:receiptId/retry", () =>
          HttpResponse.json(
            { id: 7, status: "processing", stage: "queued" },
            { status },
          ),
        ),
      );
      const queryClient = createTestQueryClient();
      queryClient.setQueryData(
        receiptKeys.detail(7),
        receiptDetail({
          status: "failed",
          stage: "failed",
          failure_reason: "unknown",
        }),
      );
      const invalidate = vi.spyOn(queryClient, "invalidateQueries");
      const { result } = renderHook(() => useRetryReceipt(7), {
        wrapper: ({ children }: { children: ReactNode }) => (
          <QueryClientProvider client={queryClient}>
            {children}
          </QueryClientProvider>
        ),
      });

      await act(async () => {
        await result.current.mutateAsync().catch(() => undefined);
      });

      expect(
        invalidate.mock.calls.map(([filters]) => filters?.queryKey),
      ).toEqual([receiptKeys.detail(7), receiptKeys.list()]);
      expect(queryClient.getQueryData(receiptKeys.detail(7))).toMatchObject(
        status === 202
          ? { status: "processing", stage: "queued", failure_reason: null }
          : { status: "failed", failure_reason: "unknown" },
      );
    },
  );
});
