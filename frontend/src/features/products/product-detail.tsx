import { useIsMutating } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { ArrowLeft, SearchX, TriangleAlert } from "lucide-react";
import { useState } from "react";

import { ApiError } from "@/api/client";
import { EmptyState } from "@/components/empty-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  hasSuggestion,
  productKeys,
  type ProductOut,
  type ProductPurchaseOut,
  sizeText,
  useCategories,
  useProduct,
  useProductPurchases,
} from "@/features/products/api";
import { ProductEditor } from "@/features/products/product-editor";
import { formatDate } from "@/features/receipts/dates";
import { formatMoney } from "@/features/receipts/money";

const SKELETON_ROWS = 3;

/** One product: what it is, an editor behind Edit, and every purchase of it. */
export function ProductDetail({ productId }: { productId: number }) {
  const product = useProduct(productId);

  if (product.isPending) return <ProductDetailSkeleton />;
  if (product.isError) {
    const notFound =
      product.error instanceof ApiError && product.error.status === 404;
    return notFound ? (
      <ProductNotFound />
    ) : (
      <EmptyState
        icon={TriangleAlert}
        title="Couldn't open this product"
        description="Paper Trail could not reach the server. Pull to refresh or try again later."
      />
    );
  }
  return <ProductView key={product.data.id} product={product.data} />;
}

export function ProductNotFound() {
  return (
    <EmptyState
      icon={SearchX}
      title="Product not found"
      description="This product does not exist. It may have been merged, or the link is wrong."
    />
  );
}

function ProductView({ product }: { product: ProductOut }) {
  const [editing, setEditing] = useState(false);
  const saving =
    useIsMutating({ mutationKey: productKeys.detail(product.id) }) > 0;
  const categories = useCategories();
  const category =
    categories.data?.find((option) => option.value === product.category)
      ?.label ?? product.category;
  const details = [product.brand, sizeText(product), category]
    .filter(Boolean)
    .join(" · ");

  return (
    <article className="flex flex-col gap-6 px-4 py-4">
      <Link
        to="/products"
        className="flex w-fit items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft aria-hidden="true" className="size-4" />
        Products
      </Link>
      <section className="flex flex-col gap-4 rounded-xl border bg-card p-4">
        <header className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-xl font-semibold tracking-tight break-words">
                {product.canonical_name}
              </h1>
              {hasSuggestion(product) ? (
                <Badge variant="outline" className="border-warning">
                  Suggested
                </Badge>
              ) : null}
            </div>
            {details ? (
              <p className="text-sm text-muted-foreground">{details}</p>
            ) : null}
          </div>
          <Button
            type="button"
            variant="outline"
            aria-expanded={editing}
            disabled={saving}
            onClick={() => {
              setEditing((value) => !value);
            }}
          >
            {editing ? "Cancel" : "Edit"}
          </Button>
        </header>
        {editing ? <ProductEditor product={product} /> : null}
      </section>
      <ProductPurchases productId={product.id} />
    </article>
  );
}

function paidPerUnit(purchase: ProductPurchaseOut): number {
  return purchase.paid.amount_cents / Number(purchase.quantity);
}

function ProductPurchases({ productId }: { productId: number }) {
  const purchases = useProductPurchases(productId);

  if (purchases.isPending) {
    return (
      <div aria-busy="true" aria-label="Loading purchases">
        <Skeleton className="mb-3 h-5 w-32" />
        <Skeleton className="h-32 w-full" />
      </div>
    );
  }
  if (purchases.isError) {
    return (
      <p role="alert" className="text-sm text-destructive">
        Couldn&apos;t load this product&apos;s purchases.
      </p>
    );
  }

  const rows = purchases.data;
  const first = rows[0];
  const perUnit = rows
    .filter((purchase) => purchase.paid.currency === first?.paid.currency)
    .map(paidPerUnit);
  return (
    <section
      aria-labelledby="purchases-heading"
      className="flex flex-col gap-3"
    >
      <h2 id="purchases-heading" className="font-semibold">
        Purchases ({rows.length})
      </h2>
      {first && perUnit.length >= 2 ? (
        <p className="text-sm text-muted-foreground">
          Lowest{" "}
          {formatMoney({
            ...first.paid,
            amount_cents: Math.round(Math.min(...perUnit)),
          })}{" "}
          · average{" "}
          {formatMoney({
            ...first.paid,
            amount_cents: Math.round(
              perUnit.reduce((sum, cents) => sum + cents, 0) / perUnit.length,
            ),
          })}{" "}
          per unit
        </p>
      ) : null}
      {rows.length === 0 ? (
        <p className="rounded-xl border border-dashed px-4 py-6 text-center text-sm text-muted-foreground">
          No purchases yet. Scan a receipt with this product to see what you
          paid.
        </p>
      ) : (
        <ul className="divide-y rounded-xl border bg-card">
          {rows.map((purchase, index) => (
            <li key={`${String(purchase.receipt_id)}-${String(index)}`}>
              <PurchaseRow purchase={purchase} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function PurchaseRow({ purchase }: { purchase: ProductPurchaseOut }) {
  const notes = [
    Number(purchase.quantity) === 1 ? null : `× ${purchase.quantity}`,
    purchase.discount.amount_cents === 0
      ? null
      : `save ${formatMoney({ ...purchase.discount, amount_cents: -purchase.discount.amount_cents })}`,
  ].filter(Boolean);
  return (
    <Link
      to="/receipts/$receiptId"
      params={{ receiptId: String(purchase.receipt_id) }}
      className="flex min-h-14 items-center justify-between gap-3 px-4 py-3 hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
    >
      <div className="min-w-0">
        <p className="truncate text-sm">
          {purchase.purchased_on === null
            ? "Date unknown"
            : formatDate(purchase.purchased_on)}{" "}
          · {purchase.store_name}
        </p>
        {notes.length > 0 ? (
          <p className="truncate text-sm text-muted-foreground">
            {notes.join(" · ")}
          </p>
        ) : null}
      </div>
      <span className="shrink-0 font-medium tabular-nums">
        {formatMoney(purchase.paid)}
      </span>
    </Link>
  );
}

function ProductDetailSkeleton() {
  return (
    <div
      aria-busy="true"
      aria-label="Loading product"
      className="flex flex-col gap-4 px-4 py-4"
    >
      <Skeleton className="h-7 w-48" />
      <Skeleton className="h-4 w-32" />
      {Array.from({ length: SKELETON_ROWS }, (_, index) => (
        <Skeleton key={index} className="h-14 w-full" />
      ))}
    </div>
  );
}
