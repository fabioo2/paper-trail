import { Link } from "@tanstack/react-router";
import { ArrowLeft, CircleX, SearchX, TriangleAlert } from "lucide-react";

import { ApiError } from "@/api/client";
import { EmptyState } from "@/components/empty-state";
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
  const lines = receipt.items.length;
  return (
    <article className="flex flex-col gap-6 px-4 py-4">
      <Link
        to="/receipts"
        className="flex w-fit items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft aria-hidden="true" className="size-4" />
        Receipts
      </Link>
      <header className="flex flex-col gap-1">
        <div className="flex items-start justify-between gap-3">
          <h1 className="text-xl font-semibold tracking-tight break-words">
            {displayStoreName(receipt.store_name)}
          </h1>
          <ReceiptStatusBadge status={receipt.status} />
        </div>
        <p className="text-sm text-muted-foreground">
          {receipt.purchased_on === null
            ? "Date unknown"
            : formatFullDate(receipt.purchased_on)}{" "}
          · {lines} {lines === 1 ? "line" : "lines"}
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

      <ul aria-label="Items" className="divide-y rounded-xl border bg-card">
        {receiptRows(receipt.items).map((row) => (
          <li key={row.key}>
            <LineRow row={row} categories={categories.data} />
          </li>
        ))}
      </ul>

      <dl className="ml-auto grid w-full max-w-xs grid-cols-[1fr_auto] gap-x-6 gap-y-1 text-sm">
        <dt className="text-muted-foreground">Subtotal</dt>
        <dd className="text-right tabular-nums">{money(receipt.subtotal)}</dd>
        <dt className="text-muted-foreground">Tax</dt>
        <dd className="text-right tabular-nums">{money(receipt.tax)}</dd>
        <dt className="font-semibold">Total</dt>
        <dd className="text-right font-semibold tabular-nums">
          {money(receipt.total)}
        </dd>
      </dl>
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
    <div className="flex flex-1 flex-col rounded-xl border bg-card px-4 py-3">
      <span className="text-sm text-muted-foreground">{label}</span>
      <span className={cn("text-xl font-semibold tabular-nums", className)}>
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
  const content = (
    <>
      <div className="min-w-0">
        <p className="font-medium break-words">
          {row.name}
          {Number(row.quantity) === 1 ? null : ` ×${row.quantity}`}
        </p>
        {row.printed !== null ? (
          <p className="truncate font-mono text-xs text-muted-foreground">
            {row.printed}
          </p>
        ) : category !== null ? (
          <p className="flex items-center gap-1.5 text-sm text-muted-foreground">
            <CategoryDot category={row.category} />
            {category}
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
    "flex min-h-14 items-center justify-between gap-3 px-4 py-3";
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
