import { Link } from "@tanstack/react-router";
import { ChevronRight, ClipboardCheck, CloudOff } from "lucide-react";
import { useEffect } from "react";
import { toast } from "sonner";

import { EmptyState } from "@/components/empty-state";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { useProductsToCheck } from "@/features/products/api";
import { formatDate } from "@/features/receipts/dates";
import { formatMoney } from "@/features/receipts/money";
import { type ReviewSummaryOut, useReviewList } from "@/features/reviews/api";
import { formatAge, reasonText } from "@/features/reviews/draft-form";

const SKELETON_ROWS = 4;
const LOAD_ERROR_TOAST = "reviews-load-error";

/** The Review tab: the products to check, then every receipt waiting for a person, newest first. */
export function ReviewList() {
  const reviews = useReviewList();
  const toCheck = useProductsToCheck();
  const { isError, refetch } = reviews;

  useEffect(() => {
    if (!isError) {
      toast.dismiss(LOAD_ERROR_TOAST);
      return;
    }
    toast.error("Couldn't load reviews", {
      id: LOAD_ERROR_TOAST,
      duration: Infinity,
      action: {
        label: "Retry",
        onClick: () => {
          void refetch();
        },
      },
    });
  }, [isError, refetch]);

  const products = toCheck.isError ? (
    <ProductsToCheckRow count={null} />
  ) : toCheck.data?.length ? (
    <ProductsToCheckRow count={toCheck.data.length} />
  ) : null;

  if (reviews.isPending) return <ReviewListSkeleton />;
  if (reviews.isError) {
    return (
      <>
        {products ? <div className="px-4 pt-4">{products}</div> : null}
        <EmptyState
          icon={CloudOff}
          title="Reviews are unavailable"
          description="Paper Trail could not reach the server. Retry from the notice once you are back online."
        />
      </>
    );
  }
  if (reviews.data.length === 0 && !products) {
    if (toCheck.isPending) return <ReviewListSkeleton />;
    return (
      <EmptyState
        icon={ClipboardCheck}
        title="Nothing to review"
        description="Receipts that need a second look, such as totals that do not add up, wait here for you to check."
      />
    );
  }

  return (
    <section
      aria-labelledby="reviews-heading"
      className="flex flex-col gap-3 px-4 py-4"
    >
      <h1 id="reviews-heading" className="text-xl font-semibold tracking-tight">
        To review
      </h1>
      {products}
      {reviews.data.length > 0 ? (
        <ul className="divide-y rounded-xl border bg-card">
          {reviews.data.map((review) => (
            <li key={review.id}>
              <ReviewRow review={review} />
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

/** The way into the product queue; `count` is null when the list could not be read. */
function ProductsToCheckRow({ count }: { count: number | null }) {
  return (
    <Link
      to="/products/check"
      className="flex min-h-16 items-center justify-between gap-3 rounded-xl border bg-card px-4 py-3 hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
    >
      <div className="min-w-0">
        <p className="font-medium">
          Products to check{count === null ? "" : ` (${String(count)})`}
        </p>
        <p className="truncate text-sm text-muted-foreground">
          {count === null
            ? "Couldn't count them right now. Open the list to try again."
            : "Confirm or fix what Claude guessed, one product at a time."}
        </p>
      </div>
      <ChevronRight
        aria-hidden="true"
        className="size-5 shrink-0 text-muted-foreground"
      />
    </Link>
  );
}

function ReviewRow({ review }: { review: ReviewSummaryOut }) {
  const reading = review.reading;
  const issues = review.issues.length;
  return (
    <Link
      to="/review/$reviewId"
      params={{ reviewId: String(review.id) }}
      className="flex min-h-16 items-center justify-between gap-3 px-4 py-3 hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
    >
      <div className="min-w-0">
        <p className="truncate font-medium">
          {reading === null || reading.store_name === ""
            ? "Unreadable receipt"
            : reading.store_name}
        </p>
        <p className="truncate text-sm text-muted-foreground">
          {reading?.purchased_on
            ? formatDate(reading.purchased_on)
            : "Date unknown"}{" "}
          · {formatAge(review.created_at)}
        </p>
        <p className="flex items-center gap-2 text-sm">
          <Badge variant="outline" className="border-warning">
            {reasonText(review)}
          </Badge>
          {issues > 0 ? (
            <span className="text-muted-foreground">
              {issues === 1 ? "1 issue" : `${String(issues)} issues`}
            </span>
          ) : null}
        </p>
      </div>
      {reading === null ? null : (
        <span className="shrink-0 font-medium tabular-nums">
          {formatMoney(reading.total)}
        </span>
      )}
    </Link>
  );
}

function ReviewListSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading reviews" className="px-4 py-4">
      <div className="divide-y rounded-xl border bg-card">
        {Array.from({ length: SKELETON_ROWS }, (_, index) => (
          <div
            key={index}
            className="flex min-h-16 items-center justify-between gap-3 px-4 py-3"
          >
            <div className="flex flex-col gap-2">
              <Skeleton className="h-4 w-36" />
              <Skeleton className="h-3 w-24" />
            </div>
            <Skeleton className="h-4 w-16" />
          </div>
        ))}
      </div>
    </div>
  );
}
