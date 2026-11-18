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
import { formatMonth, formatWeekday } from "@/features/receipts/dates";
import { formatMoney, type MoneyOut } from "@/features/receipts/money";
import { ReceiptStatusBadge } from "@/features/receipts/receipt-status-badge";
import { displayStoreName } from "@/features/receipts/store-name";

const SKELETON_ROWS = 6;
const LOAD_ERROR_TOAST = "receipts-load-error";

/**
 * The Receipts tab: every receipt by purchase date, newest first, under a heading per month
 * with its total, loading more as the end scrolls into view.
 */
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
    <section
      aria-labelledby="receipts-heading"
      className="flex flex-col gap-4 px-4 py-4"
    >
      <h1 id="receipts-heading" className="sr-only">
        Receipts
      </h1>
      {groupByMonth(rows).map((month) => (
        <section key={month.key} aria-label={month.label}>
          <h2 className="mb-2 flex items-baseline justify-between gap-3 px-1">
            <span className="font-semibold">{month.label}</span>
            <span className="text-sm text-muted-foreground tabular-nums">
              {[
                month.totals.map((sum) => formatMoney(sum)).join(" + "),
                `${String(month.receipts.length)} ${month.receipts.length === 1 ? "trip" : "trips"}`,
              ]
                .filter(Boolean)
                .join(" · ")}
            </span>
          </h2>
          <ul className="divide-y rounded-xl border bg-card">
            {month.receipts.map((receipt) => (
              <li key={receipt.id}>
                <ReceiptRow receipt={receipt} />
              </li>
            ))}
          </ul>
        </section>
      ))}
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

interface Month {
  /** `YYYY-MM`, or "undated" for the receipts without a date (listed last). */
  key: string;
  label: string;
  /** The month's receipt totals, one sum per currency in the order first seen. */
  totals: MoneyOut[];
  receipts: ReceiptSummaryOut[];
}

/**
 * The receipts (newest purchase first) under one heading per month. A month seen again, e.g.
 * after a date was corrected between two pages, joins its heading instead of repeating it.
 */
function groupByMonth(receipts: ReceiptSummaryOut[]): Month[] {
  const months = new Map<string, Month>();
  for (const receipt of receipts) {
    const key = receipt.purchased_on?.slice(0, 7) ?? "undated";
    let month = months.get(key);
    if (month === undefined) {
      month = {
        key,
        label:
          receipt.purchased_on === null
            ? "No date"
            : formatMonth(receipt.purchased_on),
        totals: [],
        receipts: [],
      };
      months.set(key, month);
    }
    month.receipts.push(receipt);
    const total = receipt.total;
    if (total === null) continue;
    const index = month.totals.findIndex(
      (sum) => sum.currency === total.currency,
    );
    const sum = month.totals[index];
    if (sum === undefined) month.totals.push(total);
    else {
      month.totals[index] = {
        ...sum,
        amount_cents: sum.amount_cents + total.amount_cents,
      };
    }
  }
  return [...months.values()];
}

function ReceiptRow({ receipt }: { receipt: ReceiptSummaryOut }) {
  const items = `${String(receipt.item_count)} ${receipt.item_count === 1 ? "item" : "items"}`;
  return (
    <Link
      to="/receipts/$receiptId"
      params={{ receiptId: String(receipt.id) }}
      className="flex min-h-16 items-center justify-between gap-3 px-4 py-3 hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
    >
      <div className="flex min-w-0 items-center gap-3">
        <div className="flex w-8 shrink-0 flex-col items-center leading-tight">
          {receipt.purchased_on === null ? (
            <span className="text-muted-foreground">—</span>
          ) : (
            <>
              <span className="font-semibold tabular-nums">
                {Number(receipt.purchased_on.slice(8, 10))}
              </span>
              <span className="text-xs text-muted-foreground">
                {formatWeekday(receipt.purchased_on)}
              </span>
            </>
          )}
        </div>
        <div className="min-w-0">
          <p className="truncate font-medium">
            {displayStoreName(receipt.store_name)}
          </p>
          <p className="truncate text-sm text-muted-foreground">
            {receipt.savings.amount_cents > 0
              ? `${items} · saved ${formatMoney(receipt.savings)}`
              : items}
          </p>
        </div>
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
