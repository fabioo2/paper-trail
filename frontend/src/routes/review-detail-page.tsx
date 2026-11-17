import { useParams } from "@tanstack/react-router";

import { ReviewEditor, ReviewNotFound } from "@/features/reviews/review-editor";

export function ReviewDetailPage() {
  const { reviewId } = useParams({ from: "/review/$reviewId" });
  const id = Number(reviewId);
  // Only positive integer ids exist; anything else in the URL is a review that is not there.
  return Number.isSafeInteger(id) && id > 0 ? (
    <ReviewEditor reviewId={id} />
  ) : (
    <ReviewNotFound />
  );
}
