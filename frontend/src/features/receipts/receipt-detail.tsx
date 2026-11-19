import { Link } from "@tanstack/react-router";
import { ArrowLeft, CircleX, SearchX, TriangleAlert } from "lucide-react";

import { ApiError } from "@/api/client";
import { EmptyState } from "@/components/empty-state";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { type CategoryOut, useCategories } from "@/features/products/api";
import { CategoryDot } from "@/features/products/category-dot";
import { type ReceiptDetailOut, useReceipt } from "@/features/receipts/api";
import { formatFullDate } from "@/features/receipts/dates";
import { formatMoney, type MoneyOut } from "@/features/receipts/money";
import { failureMessage } from "@/features/receipts/failure-reasons";
import { RetryButton } from "@/features/receipts/receipt-failure";
import {
  type ReceiptRow,
  receiptRows,
} from "@/features/receipts/receipt-lines";
import { ReceiptStatusBadge } from "@/features/receipts/receipt-status-badge";
import { displayStoreName } from "@/features/receipts/store-name";
import { cn } from "@/lib/utils";

const NO_VALUE = "—";

function money(value: MoneyOut | null): string {
  return value === null ? NO_VALUE : formatMoney(value);
}

/** One receipt: header, its lines as read, totals and, when it does not add up, why. */
export function ReceiptDetail({ receiptId }: { receiptId: number }) {
  const receipt = useReceipt(receiptId);

  if (receipt.isPending) return <ReceiptDetailSkeleton />;
  if (receipt.isError) {
    const notFound =
      receipt.error instanceof ApiError && receipt.error.status === 404;
    return notFound ? (
      <ReceiptNotFound />
    ) : (
      <EmptyState
        icon={TriangleAlert}
        title="Couldn't open this receipt"
        description="Paper Trail could not reach the server. Pull to refresh or try again later."
      />
    );
  }
  return <ReceiptView receipt={receipt.data} />;
}

export function ReceiptNotFound() {
  return (
    <EmptyState
      icon={SearchX}
      title="Receipt not found"
      description="This receipt does not exist. It may have been removed, or the link is wrong."
    />
  );
}

function ReceiptView({ receipt }: { receipt: ReceiptDetailOut }) {
  const categories = useCategories();
  const items = receipt.items.filter((line) => line.kind === "item").length;
  return (
    <article className="flex flex-col gap-4 px-4 py-4">
      <Link
        to="/receipts"
        className="flex w-fit items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft aria-hidden="true" className="size-4" />
        Receipts
      </Link>
      <header className="flex flex-col gap-1">
        <div className="flex items-start justify-between gap-3">
          <h1 className="text-lg font-semibold tracking-tight break-words">
            {displayStoreName(receipt.store_name)}
          </h1>
          <ReceiptStatusBadge status={receipt.status} />
        </div>
        <p className="text-sm text-muted-foreground">
          {receipt.purchased_on === null
            ? "Date unknown"
            : formatFullDate(receipt.purchased_on)}{" "}
          · {items} {items === 1 ? "item" : "items"}
        </p>
      </header>

      {receipt.status === "failed" ? (
        <section
          aria-labelledby="failure-heading"
          className="flex flex-col gap-3 rounded-xl border border-destructive bg-destructive/10 p-4"
        >
          <div>
            <h2
              id="failure-heading"
              className="flex items-center gap-2 font-semibold"
            >
              <CircleX aria-hidden="true" className="size-4" />
              Couldn&apos;t read this receipt
            </h2>
            <p className="mt-1 text-sm">
              {failureMessage(receipt.failure_reason)}
            </p>
          </div>
          <RetryButton receiptId={receipt.id} />
        </section>
      ) : null}

      {receipt.status === "needs_review" && receipt.review_id !== null ? (
        <Button asChild size="lg" className="h-12 w-full">
          <Link
            to="/review/$reviewId"
            params={{ reviewId: String(receipt.review_id) }}
          >
            Review
          </Link>
        </Button>
      ) : null}

      <div className="flex gap-3">
        <Tile label="Total" value={money(receipt.total)} />
        {receipt.savings.amount_cents > 0 ? (
          <Tile
            label="You saved"
            value={formatMoney(receipt.savings)}
            className="text-success"
          />
        ) : null}
      </div>

      {receipt.arithmetic.ok ? null : (
        <section
          aria-labelledby="arithmetic-heading"
          className="rounded-xl border border-warning bg-warning/10 p-4"
        >
          <h2
            id="arithmetic-heading"
            className="flex items-center gap-2 font-semibold"
          >
            <TriangleAlert aria-hidden="true" className="size-4" />
            The numbers don&apos;t add up
          </h2>
          <ul className="mt-2 list-disc pl-5 text-sm">
            {receipt.arithmetic.issues.map((issue, index) => (
              // Neither code nor position is unique per issue; the list never reorders.
              <li key={index}>{issue.message}</li>
            ))}
          </ul>
        </section>
      )}

      <div className="divide-y rounded-xl border bg-card">
        <ul aria-label="Items" className="divide-y">
          {receiptRows(receipt.items).map((row) => (
            <li key={row.key}>
              <LineRow row={row} categories={categories.data} />
            </li>
          ))}
        </ul>
        <dl className="grid grid-cols-[1fr_auto] gap-x-6 gap-y-1 px-4 py-3 text-sm">
          <dt className="text-muted-foreground">Subtotal</dt>
          <dd className="text-right tabular-nums">{money(receipt.subtotal)}</dd>
          <dt className="text-muted-foreground">Tax</dt>
          <dd className="text-right tabular-nums">{money(receipt.tax)}</dd>
          <dt className="font-semibold">Total</dt>
          <dd className="text-right font-semibold tabular-nums">
            {money(receipt.total)}
          </dd>
        </dl>
      </div>
    </article>
  );
}

function Tile({
  label,
  value,
  className,
}: {
  label: string;
  value: string;
  className?: string;
}) {
  return (
    <div className="flex flex-1 flex-col rounded-xl border bg-card px-4 py-2">
      <span className="text-sm text-muted-foreground">{label}</span>
      <span className={cn("text-lg font-semibold tabular-nums", className)}>
        {value}
      </span>
    </div>
  );
}

function LineRow({
  row,
  categories,
}: {
  row: ReceiptRow;
  categories: CategoryOut[] | undefined;
}) {
  const category =
    row.category === null
      ? null
      : (categories?.find((option) => option.value === row.category)?.label ??
        row.category);
  const unit = row.unit === "kg" || row.unit === "lb" ? row.unit : null;
  const weight = unit === null ? row.quantity : `${row.quantity} ${unit}`;
  const details = [
    !row.weighed
      ? null
      : row.pricePerUnit === null
        ? weight
        : `${weight} @ ${formatMoney(row.pricePerUnit)}${unit === null ? "" : `/${unit}`}`,
    category ?? row.printed,
  ].filter(Boolean);
  const content = (
    <>
      <div className="min-w-0">
        <p className="font-medium break-words">
          {row.name}
          {!row.weighed && Number(row.quantity) !== 1
            ? ` ×${row.quantity}`
            : null}
        </p>
        {details.length > 0 ? (
          <p className="flex items-center gap-1.5 text-sm text-muted-foreground">
            <CategoryDot category={row.category} />
            <span className="truncate">{details.join(" · ")}</span>
          </p>
        ) : null}
      </div>
      <div className="flex shrink-0 flex-col items-end">
        <span className="font-medium tabular-nums">
          {formatMoney(row.paid)}
        </span>
        {row.saved === null ? null : (
          <span className="text-sm text-success tabular-nums">
            saved {formatMoney(row.saved)}
          </span>
        )}
      </div>
    </>
  );
  const className =
    "flex min-h-12 items-center justify-between gap-3 px-4 py-2";
  return row.productId === null ? (
    <div className={className}>{content}</div>
  ) : (
    <Link
      to="/products/$productId"
      params={{ productId: String(row.productId) }}
      className={cn(
        className,
        "hover:bg-accent focus-visible:bg-accent focus-visible:outline-none",
      )}
    >
      {content}
    </Link>
  );
}

function ReceiptDetailSkeleton() {
  return (
    <div
      aria-busy="true"
      aria-label="Loading receipt"
      className="flex flex-col gap-4 px-4 py-4"
    >
      <Skeleton className="h-7 w-48" />
      <Skeleton className="h-4 w-32" />
      <Skeleton className="h-40 w-full" />
    </div>
  );
}
