import { useIsMutating } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { ArrowLeft, SearchX, TriangleAlert } from "lucide-react";
import { useState } from "react";

import { ApiError } from "@/api/client";
import { EmptyState } from "@/components/empty-state";
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
import { CategoryDot } from "@/features/products/category-dot";
import { GuessBadge } from "@/features/products/field-source";
import { PriceHistory } from "@/features/products/price-history";
import { paidPerUnit, unitSuffix } from "@/features/products/prices";
import { ProductEditor } from "@/features/products/product-editor";
import { ProductMerge } from "@/features/products/product-merge";
import { formatDate } from "@/features/receipts/dates";
import { formatMoney } from "@/features/receipts/money";
import { displayStoreName } from "@/features/receipts/store-name";

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
  const [open, setOpen] = useState<"edit" | "merge" | null>(null);
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
            <div>
              <h1 className="inline text-xl font-semibold tracking-tight break-words">
                {product.canonical_name}
              </h1>
              {hasSuggestion(product) ? (
                <GuessBadge className="ml-2 align-middle" />
              ) : null}
            </div>
            {details ? (
              <p className="flex items-center gap-1.5 text-sm text-muted-foreground">
                <CategoryDot category={product.category} />
                {details}
              </p>
            ) : null}
          </div>
          <Button
            type="button"
            variant="outline"
            aria-expanded={open === "edit"}
            disabled={saving || open === "merge"}
            onClick={() => {
              setOpen((value) => (value === "edit" ? null : "edit"));
            }}
          >
            {open === "edit" ? "Cancel" : "Edit"}
          </Button>
        </header>
        <div>
          <Button
            type="button"
            size="sm"
            variant="outline"
            aria-expanded={open === "merge"}
            disabled={saving || open === "edit"}
            onClick={() => {
              setOpen((value) => (value === "merge" ? null : "merge"));
            }}
          >
            {open === "merge" ? "Cancel" : "Same product as…"}
          </Button>
        </div>
        {open === "edit" ? <ProductEditor product={product} /> : null}
        {open === "merge" ? (
          <ProductMerge
            product={product}
            onClose={() => {
              setOpen(null);
            }}
          />
        ) : null}
      </section>
      <ProductPurchases product={product} />
    </article>
  );
}

function ProductPurchases({ product }: { product: ProductOut }) {
  const purchases = useProductPurchases(product.id);

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
  return (
    <>
      <PriceHistory product={product} purchases={rows} />
      <section
        aria-labelledby="purchases-heading"
        className="flex flex-col gap-3"
      >
        <h2 id="purchases-heading" className="font-semibold">
          Purchases ({rows.length})
        </h2>
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
    </>
  );
}

function PurchaseRow({ purchase }: { purchase: ProductPurchaseOut }) {
  const suffix = unitSuffix(purchase.unit);
  const notes = [
    suffix
      ? `${purchase.quantity} ${purchase.unit ?? ""} · ${formatMoney(paidPerUnit(purchase))}${suffix}`
      : Number(purchase.quantity) === 1
        ? null
        : `× ${purchase.quantity}`,
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
          · {displayStoreName(purchase.store_name)}
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
