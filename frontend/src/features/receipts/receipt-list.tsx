import { Link } from "@tanstack/react-router";
import { CloudOff, ReceiptText } from "lucide-react";
import { useEffect, useEffectEvent, useRef } from "react";
import { toast } from "sonner";

import { EmptyState } from "@/components/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import {
  type ReceiptSummaryOut,
  useReceiptList,
} from "@/features/receipts/api";
import { formatDate } from "@/features/receipts/dates";
import { formatMoney } from "@/features/receipts/money";
import { ReceiptStatusBadge } from "@/features/receipts/receipt-status-badge";

const SKELETON_ROWS = 6;
const LOAD_ERROR_TOAST = "receipts-load-error";

/** The Receipts tab: every receipt newest first, loading more as the end scrolls into view. */
export function ReceiptList() {
  const receipts = useReceiptList();
  const { isError, isFetchNextPageError, refetch, fetchNextPage } = receipts;

  useEffect(() => {
    if (!isError) {
      toast.dismiss(LOAD_ERROR_TOAST);
      return;
    }
    toast.error("Couldn't load receipts", {
      id: LOAD_ERROR_TOAST,
      duration: Infinity,
      action: {
        label: "Retry",
        onClick: () => {
          // A failed next page keeps the pages already shown and asks only for the missing one.
          void (isFetchNextPageError ? fetchNextPage() : refetch());
        },
      },
    });
  }, [isError, isFetchNextPageError, refetch, fetchNextPage]);

  if (receipts.isPending) return <ReceiptListSkeleton />;
  // No pages at all means the first one failed; a failed later page keeps the rows (toast only).
  if (receipts.data === undefined) {
    return (
      <EmptyState
        icon={CloudOff}
        title="Receipts are unavailable"
        description="Paper Trail could not reach the server. Retry from the notice once you are back online."
      />
    );
  }

  const rows = receipts.data.pages.flatMap((page) => page.items);
  if (rows.length === 0) {
    return (
      <EmptyState
        icon={ReceiptText}
        title="No receipts yet"
        description="Every receipt you scan shows up here with its store, date and total."
      />
    );
  }

  return (
    <section aria-labelledby="receipts-heading" className="px-4 py-4">
      <h1 id="receipts-heading" className="sr-only">
        Receipts
      </h1>
      <ul className="divide-y rounded-xl border bg-card">
        {rows.map((receipt) => (
          <li key={receipt.id}>
            <ReceiptRow receipt={receipt} />
          </li>
        ))}
      </ul>
      {receipts.hasNextPage ? (
        <LoadMoreSentinel
          // A new observer per page: one still in view after a page loads reports it again.
          key={receipts.data.pages.length}
          loading={receipts.isFetchingNextPage}
          onVisible={() => {
            if (!receipts.isFetchingNextPage) void receipts.fetchNextPage();
          }}
        />
      ) : null}
    </section>
  );
}

function ReceiptRow({ receipt }: { receipt: ReceiptSummaryOut }) {
  return (
    <Link
      to="/receipts/$receiptId"
      params={{ receiptId: String(receipt.id) }}
      className="flex min-h-16 items-center justify-between gap-3 px-4 py-3 hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
    >
      <div className="min-w-0">
        <p className="truncate font-medium">{receipt.store_name}</p>
        <p className="text-sm text-muted-foreground">
          {receipt.purchased_on === null
            ? "Date unknown"
            : formatDate(receipt.purchased_on)}
        </p>
      </div>
      <div className="flex shrink-0 flex-col items-end gap-1">
        <span className="font-medium tabular-nums">
          {receipt.total === null ? "—" : formatMoney(receipt.total)}
        </span>
        <ReceiptStatusBadge status={receipt.status} />
      </div>
    </Link>
  );
}

/** Invisible marker after the last row; seeing it asks for the next page. */
function LoadMoreSentinel({
  loading,
  onVisible,
}: {
  loading: boolean;
  onVisible: () => void;
}) {
  const marker = useRef<HTMLDivElement>(null);
  const handleVisible = useEffectEvent(onVisible);

  useEffect(() => {
    const element = marker.current;
    if (!element) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) handleVisible();
    });
    observer.observe(element);
    return () => {
      observer.disconnect();
    };
  }, []);

  return (
    <div ref={marker} className="py-3">
      {loading ? <Skeleton className="h-16 w-full rounded-xl" /> : null}
    </div>
  );
}

function ReceiptListSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading receipts" className="px-4 py-4">
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
