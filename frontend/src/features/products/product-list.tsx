import { Link } from "@tanstack/react-router";
import { CloudOff, Package, SearchX } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { EmptyState } from "@/components/empty-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  type CategoryOut,
  hasSuggestion,
  type ProductSummaryOut,
  sizeText,
  useCategories,
  useProductList,
} from "@/features/products/api";
import { formatDate } from "@/features/receipts/dates";
import { formatMoney } from "@/features/receipts/money";

const SKELETON_ROWS = 6;
const LOAD_ERROR_TOAST = "products-load-error";

function matches(product: ProductSummaryOut, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (needle === "") return true;
  return [product.canonical_name, product.brand ?? "", ...product.tags].some(
    (text) => text.toLowerCase().includes(needle),
  );
}

/** The Products tab: every product bought, searchable, with the unconfirmed ones a tap away. */
export function ProductList() {
  const products = useProductList();
  const categories = useCategories();
  const [query, setQuery] = useState("");
  const [needsALook, setNeedsALook] = useState(false);
  const { isError, refetch } = products;

  useEffect(() => {
    if (!isError) {
      toast.dismiss(LOAD_ERROR_TOAST);
      return;
    }
    toast.error("Couldn't load products", {
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

  if (products.isPending) return <ProductListSkeleton />;
  if (products.isError) {
    return (
      <EmptyState
        icon={CloudOff}
        title="Products are unavailable"
        description="Paper Trail could not reach the server. Retry from the notice once you are back online."
      />
    );
  }
  if (products.data.length === 0) {
    return (
      <EmptyState
        icon={Package}
        title="No products yet"
        description="Every product on the receipts you scan shows up here with where you last bought it."
      />
    );
  }

  const unconfirmed = products.data.filter((product) => !product.confirmed);
  const rows = (needsALook ? unconfirmed : products.data).filter((product) =>
    matches(product, query),
  );

  return (
    <section
      aria-labelledby="products-heading"
      className="flex flex-col gap-3 px-4 py-4"
    >
      <h1 id="products-heading" className="sr-only">
        Products
      </h1>
      <div className="flex gap-2">
        <Input
          type="search"
          aria-label="Search products"
          placeholder="Search by name, brand or tag"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
          }}
          className="h-11"
        />
        <Button
          type="button"
          variant={needsALook ? "default" : "outline"}
          aria-pressed={needsALook}
          className="h-11 shrink-0"
          onClick={() => {
            setNeedsALook((value) => !value);
          }}
        >
          Needs a look
          <Badge variant={needsALook ? "secondary" : "outline"}>
            {unconfirmed.length}
          </Badge>
        </Button>
      </div>
      {rows.length === 0 ? (
        <EmptyState
          icon={SearchX}
          title="No matching products"
          description="Try another name, brand or tag, or show every product."
        />
      ) : (
        <ul className="divide-y rounded-xl border bg-card">
          {rows.map((product) => (
            <li key={product.id}>
              <ProductRow product={product} categories={categories.data} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function ProductRow({
  product,
  categories,
}: {
  product: ProductSummaryOut;
  categories: CategoryOut[] | undefined;
}) {
  const details = [product.brand, sizeText(product)]
    .filter(Boolean)
    .join(" · ");
  const category =
    categories?.find((option) => option.value === product.category)?.label ??
    product.category;
  const last = product.last_purchase;
  return (
    <Link
      to="/products/$productId"
      params={{ productId: String(product.id) }}
      className="flex min-h-16 items-center justify-between gap-3 px-4 py-3 hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
    >
      <div className="min-w-0">
        <p className="flex items-center gap-2 font-medium">
          <span className="truncate">{product.canonical_name}</span>
          {hasSuggestion(product) ? (
            <Badge variant="outline" className="border-warning">
              Suggested
            </Badge>
          ) : null}
        </p>
        {details ? (
          <p className="truncate text-sm text-muted-foreground">{details}</p>
        ) : null}
        <p className="truncate text-sm text-muted-foreground">
          {last.purchased_on === null
            ? "Date unknown"
            : formatDate(last.purchased_on)}{" "}
          · {last.store_name}
        </p>
      </div>
      <div className="flex shrink-0 flex-col items-end gap-1">
        <span className="font-medium tabular-nums">
          {formatMoney(last.paid)}
        </span>
        {category ? (
          <span className="text-xs text-muted-foreground">{category}</span>
        ) : null}
      </div>
    </Link>
  );
}

function ProductListSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading products" className="px-4 py-4">
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
