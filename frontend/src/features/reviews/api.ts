import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { summaryKeys } from "@/features/home/api";
import { productKeys } from "@/features/products/api";
import { receiptKeys, watchReading } from "@/features/receipts/api";
import { searchKeys } from "@/features/search/api";

type Schemas = components["schemas"];
export type ReviewSummaryOut = Schemas["ReviewSummaryOut"];
export type ReviewOut = Schemas["ReviewOut"];
export type ReviewDraftBody = Schemas["ReviewDraftBody"];
export type ReviewLineBody = Schemas["ReviewLineBody"];
export type ReviewIssueOut = Schemas["ReviewIssueOut"];
export type ResolveReviewIn = Schemas["ResolveReviewIn"];

/** The problem type of a reading that does not add up: a person may keep it with a reason. */
export const INCONSISTENT_RECEIPT = "/problems/inconsistent-receipt";

export const reviewKeys = {
  all: ["reviews"] as const,
  list: () => [...reviewKeys.all, "list"] as const,
  detail: (id: number) => [...reviewKeys.all, "detail", id] as const,
};

/** The open reviews a person can answer now, newest first (`listReviews`). */
export function useReviewList() {
  return useQuery({
    queryKey: reviewKeys.list(),
    queryFn: () => unwrap(apiClient.GET("/api/reviews")),
  });
}

/** One review with the reading it holds (`getReview`). */
export function useReview(id: number) {
  return useQuery({
    queryKey: reviewKeys.detail(id),
    queryFn: () =>
      unwrap(
        apiClient.GET("/api/reviews/{review_id}", {
          params: { path: { review_id: id } },
        }),
      ),
  });
}

/** Accept the reading or send a corrected one; the worker saves it (`resolveReview`). */
export function useResolveReview(id: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ResolveReviewIn) =>
      unwrap(
        apiClient.POST("/api/reviews/{review_id}/resolve", {
          params: { path: { review_id: id } },
          body,
        }),
      ),
    onSuccess: (answer) => {
      watchReading(queryClient, answer.receipt_id, answer);
      return Promise.all([
        queryClient.invalidateQueries({ queryKey: reviewKeys.all }),
        queryClient.invalidateQueries({ queryKey: receiptKeys.all }),
        queryClient.invalidateQueries({ queryKey: productKeys.all }),
        queryClient.invalidateQueries({ queryKey: summaryKeys.all }),
        queryClient.invalidateQueries({ queryKey: searchKeys.all }),
      ]);
    },
  });
}
