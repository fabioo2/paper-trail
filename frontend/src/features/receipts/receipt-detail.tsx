import { SearchX, TriangleAlert } from "lucide-react";

import { ApiError } from "@/api/client";
import { EmptyState } from "@/components/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import {
  type LineItemOut,
  type ReceiptDetailOut,
  useReceipt,
} from "@/features/receipts/api";
import { formatDate } from "@/features/receipts/dates";
import { formatMoney, type MoneyOut } from "@/features/receipts/money";
import { ReceiptStatusBadge } from "@/features/receipts/receipt-status-badge";

const NO_VALUE = "—";

function money(value: MoneyOut | null): string {
  return value === null ? NO_VALUE : formatMoney(value);
}

/** One receipt: header, line items, totals and, when it does not add up, why. */
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
  return (
    <article className="flex flex-col gap-6 px-4 py-4">
      <header className="flex flex-col gap-2">
        <div className="flex items-start justify-between gap-3">
          <h1 className="text-xl font-semibold tracking-tight break-words">
            {receipt.store_name}
          </h1>
          <ReceiptStatusBadge status={receipt.status} />
        </div>
        <p className="flex justify-between text-sm text-muted-foreground">
          <span>
            {receipt.purchased_on === null
              ? "Date unknown"
              : formatDate(receipt.purchased_on)}
          </span>
          <span className="text-base font-semibold text-foreground tabular-nums">
            {money(receipt.total)}
          </span>
        </p>
      </header>

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

      <LineItemsTable items={receipt.items} />

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

function LineItemsTable({ items }: { items: LineItemOut[] }) {
  return (
    <table className="w-full text-sm">
      <thead className="text-left text-muted-foreground">
        <tr className="border-b">
          <th scope="col" className="py-2 pr-2 font-medium">
            Item
          </th>
          <th scope="col" className="px-2 py-2 text-right font-medium">
            Qty
          </th>
          <th scope="col" className="px-2 py-2 text-right font-medium">
            Unit price
          </th>
          <th scope="col" className="py-2 pl-2 text-right font-medium">
            Total
          </th>
        </tr>
      </thead>
      <tbody className="divide-y">
        {items.map((item) => (
          <tr key={item.id}>
            <td className="py-2 pr-2 break-words">{item.description}</td>
            <td className="px-2 py-2 text-right tabular-nums">
              {item.quantity}
            </td>
            <td className="px-2 py-2 text-right tabular-nums">
              {money(item.unit_price)}
            </td>
            <td className="py-2 pl-2 text-right tabular-nums">
              {formatMoney(item.line_total)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
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
