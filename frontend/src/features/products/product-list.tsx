import { Link, useNavigate } from "@tanstack/react-router";
import { CloudOff, Package, SearchX } from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import { toast } from "sonner";

import type { ProductsSearch } from "@/app/router";
import { EmptyState } from "@/components/empty-state";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import {
  hasSuggestion,
  type ProductSummaryOut,
  useCategories,
  useProductList,
} from "@/features/products/api";
import { CategoryDot } from "@/features/products/category-dot";
import { GuessBadge } from "@/features/products/field-source";
import { sparklinePoints } from "@/features/products/price-chart";
import {
  comparablePrices,
  type PriceChange,
  priceChange,
  sizePrice,
} from "@/features/products/prices";
import { formatMoney } from "@/features/receipts/money";
import { displayStoreName } from "@/features/receipts/store-name";

const SKELETON_ROWS = 6;
const LOAD_ERROR_TOAST = "products-load-error";
const SPARK = { width: 54, height: 20 };

function matches(product: ProductSummaryOut, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (needle === "") return true;
  return [product.canonical_name, product.brand ?? "", ...product.tags].some(
    (text) => text.toLowerCase().includes(needle),
  );
}

/** The stores a product was bought at lately, as displayed. */
function storesOf(product: ProductSummaryOut): string[] {
  return [product.last_purchase, ...product.recent].map((purchase) =>
    displayStoreName(purchase.store_name),
  );
}

function inFilter(product: ProductSummaryOut, filter: ProductsSearch): boolean {
  if (filter.needsALook) return !product.confirmed;
  if (filter.category !== undefined)
    return product.category === filter.category;
  if (filter.store !== undefined)
    return storesOf(product).includes(filter.store);
  return true;
}

/** The Products tab: every product bought with its price trend, filtered by chip and search. */
export function ProductList({ filter = {} }: { filter?: ProductsSearch }) {
  const products = useProductList();
  const categories = useCategories();
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
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

  const present = new Set(products.data.map((product) => product.category));
  const categoryChips = (categories.data ?? []).filter((option) =>
    present.has(option.value),
  );
  const stores = [...new Set(products.data.flatMap(storesOf))].sort((a, b) =>
    a.localeCompare(b),
  );
  const unconfirmed = products.data.filter((product) => !product.confirmed);
  const rows = products.data.filter(
    (product) => inFilter(product, filter) && matches(product, query),
  );
  const choose = (next: ProductsSearch) => {
    void navigate({ to: "/products", search: next, replace: true });
  };
  const all =
    !filter.needsALook &&
    filter.category === undefined &&
    filter.store === undefined;

  return (
    <section
      aria-labelledby="products-heading"
      className="flex flex-col gap-3 px-4 py-4"
    >
      <h1 id="products-heading" className="sr-only">
        Products
      </h1>
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
      <div
        role="group"
        aria-label="Filter products"
        className="-mx-4 flex [scrollbar-width:none] gap-2 overflow-x-auto px-4 pb-1 [&::-webkit-scrollbar]:hidden"
      >
        <Chip
          on={all}
          onClick={() => {
            choose({});
          }}
        >
          All
        </Chip>
        {categoryChips.map((option) => (
          <Chip
            key={option.value}
            on={filter.category === option.value}
            onClick={() => {
              choose({ category: option.value });
            }}
          >
            <CategoryDot category={option.value} />
            {option.label}
          </Chip>
        ))}
        {stores.map((store) => (
          <Chip
            key={store}
            on={filter.store === store}
            onClick={() => {
              choose({ store });
            }}
          >
            {store}
          </Chip>
        ))}
        <Chip
          on={filter.needsALook === true}
          onClick={() => {
            choose({ needsALook: true });
          }}
        >
          To check
          <Badge variant={filter.needsALook ? "secondary" : "outline"}>
            {unconfirmed.length}
          </Badge>
        </Chip>
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
              <ProductRow product={product} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function Chip({
  on,
  onClick,
  children,
}: {
  on: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={on}
      onClick={onClick}
      className={cn(
        "flex h-9 shrink-0 items-center gap-1.5 rounded-full border px-3 text-sm whitespace-nowrap",
        on
          ? "border-primary bg-primary text-primary-foreground"
          : "bg-card hover:bg-accent",
      )}
    >
      {children}
    </button>
  );
}

function ProductRow({ product }: { product: ProductSummaryOut }) {
  const last = product.last_purchase;
  const newest = product.recent[0];
  const unitPrice = newest ? sizePrice(product, newest) : null;
  const details = [
    displayStoreName(last.store_name),
    `${String(product.times_bought)}×`,
    unitPrice ? `${formatMoney(unitPrice.price)}${unitPrice.suffix}` : null,
  ]
    .filter(Boolean)
    .join(" · ");
  const trend = comparablePrices(product.recent)
    .map(({ perUnit }) => perUnit.amount_cents)
    .reverse();
  const change = priceChange(product.recent);
  return (
    <Link
      to="/products/$productId"
      params={{ productId: String(product.id) }}
      className="flex min-h-16 items-center gap-3 px-4 py-3 hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
    >
      <div className="min-w-0 flex-1">
        <p className="flex items-center gap-2 font-medium">
          <span className="truncate">{product.canonical_name}</span>
          {hasSuggestion(product) ? <GuessBadge /> : null}
        </p>
        <p className="flex items-center gap-1.5 text-sm text-muted-foreground">
          <CategoryDot category={product.category} />
          <span className="truncate">{details}</span>
        </p>
      </div>
      <Sparkline values={trend} />
      <div className="flex shrink-0 flex-col items-end">
        <span className="font-medium tabular-nums">
          {formatMoney(last.paid)}
        </span>
        <ChangeLabel change={change} firstTime={product.times_bought === 1} />
      </div>
    </Link>
  );
}

/** The price per unit over time, oldest first; an empty slot below two prices. */
function Sparkline({ values }: { values: number[] }) {
  const points = sparklinePoints(values, SPARK.width, SPARK.height);
  const end = points.at(-1);
  return (
    <svg
      viewBox={`0 0 ${String(SPARK.width)} ${String(SPARK.height)}`}
      width={SPARK.width}
      height={SPARK.height}
      aria-hidden="true"
      data-sparkline={end ? "" : undefined}
      className="shrink-0 text-muted-foreground"
    >
      {end ? (
        <>
          <polyline
            points={points
              .map(({ x, y }) => `${String(x)},${String(y)}`)
              .join(" ")}
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinejoin="round"
          />
          <circle cx={end.x} cy={end.y} r="2" fill="currentColor" />
        </>
      ) : null}
    </svg>
  );
}

/** "↓25%" (cheaper, green), "↑8%" (dearer, red), "steady", "first time", or nothing. */
function ChangeLabel({
  change,
  firstTime,
}: {
  change: PriceChange | null;
  firstTime: boolean;
}) {
  if (change === null) {
    return firstTime ? (
      <small className="text-xs text-muted-foreground">first time</small>
    ) : null;
  }
  if (change.direction === "steady") {
    return <small className="text-xs text-muted-foreground">steady</small>;
  }
  const down = change.direction === "down";
  return (
    <small
      className={cn(
        "text-xs font-medium tabular-nums",
        down ? "text-success" : "text-destructive",
      )}
    >
      <span aria-hidden="true">{down ? "↓" : "↑"}</span>
      <span className="sr-only">{down ? "down " : "up "}</span>
      {change.percent}%
    </small>
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
