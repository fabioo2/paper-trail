import { useNavigate } from "@tanstack/react-router";
import { useDeferredValue, useId, useState } from "react";
import { toast } from "sonner";

import { ApiError } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  type ProductOut,
  type ProductSummaryOut,
  sizeText,
  useMergeCandidates,
  useMergePreview,
  useMergeProduct,
  useProductPurchases,
} from "@/features/products/api";
import { SEARCH_MIN_CHARS, useSearchResults } from "@/features/search/api";
import { displayStoreName } from "@/features/receipts/store-name";

export const GONE_MESSAGE =
  "One of these products no longer exists. Pick the product again.";

/** "Same product as…": pick the product this one is the same as (suggestions first, then
 * search), then confirm. The picked product is kept and this one is merged into it. */
export function ProductMerge({
  product,
  onClose,
}: {
  product: ProductOut;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const [picked, setPicked] = useState<ProductSummaryOut | null>(null);

  return picked === null ? (
    <ProductPicker
      product={product}
      query={query}
      onQuery={setQuery}
      onPick={setPicked}
    />
  ) : (
    <MergeConfirm
      product={product}
      kept={picked}
      onBack={() => {
        setPicked(null);
      }}
      onGone={onClose}
    />
  );
}

function ProductPicker({
  product,
  query,
  onQuery,
  onPick,
}: {
  product: ProductOut;
  query: string;
  onQuery: (query: string) => void;
  onPick: (picked: ProductSummaryOut) => void;
}) {
  const id = useId();
  const searched = useDeferredValue(query.trim());
  const searching = searched.length >= SEARCH_MIN_CHARS;
  const candidates = useMergeCandidates(product.id);
  const results = useSearchResults(searching ? searched : "");
  const others = (rows: ProductSummaryOut[]) =>
    rows.filter((row) => row.id !== product.id);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1.5">
        <label htmlFor={id} className="text-sm font-medium">
          Same product as…
        </label>
        <Input
          id={id}
          type="search"
          placeholder="Search products"
          value={query}
          onChange={(event) => {
            onQuery(event.target.value);
          }}
        />
      </div>
      {searching ? (
        results.isPending ? (
          <p className="text-sm text-muted-foreground">Searching…</p>
        ) : results.isError ? (
          <p role="alert" className="text-sm text-destructive">
            Couldn&apos;t search. Try again once you are back online.
          </p>
        ) : others(results.data.products).length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No product matches “{searched}”.
          </p>
        ) : (
          <Choices
            title="Results"
            rows={others(results.data.products)}
            onPick={onPick}
          />
        )
      ) : candidates.isPending ? (
        <p className="text-sm text-muted-foreground">
          Looking for suggestions…
        </p>
      ) : candidates.isError ? (
        <p role="alert" className="text-sm text-destructive">
          Couldn&apos;t load suggestions. Search for the product instead.
        </p>
      ) : others(candidates.data).length === 0 ? (
        <p className="text-sm text-muted-foreground">
          Search for the product this one is the same as.
        </p>
      ) : (
        <Choices
          title="Suggested"
          rows={others(candidates.data)}
          onPick={onPick}
        />
      )}
    </div>
  );
}

function Choices({
  title,
  rows,
  onPick,
}: {
  title: string;
  rows: ProductSummaryOut[];
  onPick: (picked: ProductSummaryOut) => void;
}) {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="flex flex-col gap-2">
      <h3 id={headingId} className="text-sm font-medium text-muted-foreground">
        {title}
      </h3>
      <ul className="divide-y rounded-xl border">
        {rows.map((row) => (
          <li key={row.id}>
            <button
              type="button"
              className="flex min-h-14 w-full flex-col items-start px-4 py-3 text-left hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
              onClick={() => {
                onPick(row);
              }}
            >
              <span className="font-medium">{row.canonical_name}</span>
              <span className="text-sm text-muted-foreground">
                {productDetails(
                  row,
                  row.times_bought,
                  row.last_purchase.store_name,
                )}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** What tells two products apart beyond their name. */
function productDetails(
  product: ProductOut,
  timesBought: number,
  lastStore: string | undefined,
): string {
  return [
    product.brand,
    sizeText(product),
    `bought ${String(timesBought)}×`,
    lastStore === undefined ? null : `last at ${displayStoreName(lastStore)}`,
  ]
    .filter(Boolean)
    .join(" · ");
}

function MergeConfirm({
  product,
  kept,
  onBack,
  onGone,
}: {
  product: ProductOut;
  kept: ProductSummaryOut;
  onBack: () => void;
  onGone: () => void;
}) {
  const merge = useMergeProduct(product.id);
  const purchases = useProductPurchases(product.id);
  const preview = useMergePreview(product.id, kept.id);
  const navigate = useNavigate();
  const moving = purchases.data?.length;
  const renamed =
    preview.data && preview.data.canonical_name !== kept.canonical_name
      ? preview.data.canonical_name
      : null;

  const submit = () => {
    merge.mutate(kept.id, {
      onSuccess: () => {
        toast.success("Products merged");
        void navigate({
          to: "/products/$productId",
          params: { productId: String(kept.id) },
          replace: true,
        });
      },
      onError: (error) => {
        if (error instanceof ApiError && error.status === 404) {
          toast.error(GONE_MESSAGE);
          onGone();
        }
      },
    });
  };

  return (
    <div className="flex flex-col gap-3">
      <h3 className="font-medium">
        Merge “{product.canonical_name}” into “{kept.canonical_name}”?
      </h3>
      <dl className="grid gap-2 rounded-xl border p-3 text-sm">
        <div>
          <dt className="text-muted-foreground">Merged away</dt>
          <dd>
            <span className="font-medium">{product.canonical_name}</span>
            {moving === undefined
              ? null
              : ` · ${productDetails(product, moving, purchases.data?.[0]?.store_name)}`}
          </dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Kept</dt>
          <dd>
            <span className="font-medium">{kept.canonical_name}</span>
            {` · ${productDetails(kept, kept.times_bought, kept.last_purchase.store_name)}`}
          </dd>
        </div>
      </dl>
      <p className="text-sm">
        {moving === undefined
          ? "The purchases"
          : `${String(moving)} ${moving === 1 ? "purchase" : "purchases"}`}{" "}
        of “{product.canonical_name}” move to “{kept.canonical_name}”, and “
        {product.canonical_name}” is deleted.
        {renamed === null ? null : ` It is kept as “${renamed}”.`}
      </p>
      <p className="text-sm font-medium">
        This can only be undone by restoring a backup.
      </p>
      {preview.isPending ? (
        <p className="text-sm text-muted-foreground">
          Checking what the kept product will be called…
        </p>
      ) : null}
      {preview.isError ? (
        <div
          role="alert"
          className="flex items-center gap-2 text-sm text-destructive"
        >
          Couldn&apos;t check what the kept product will be called.
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => {
              void preview.refetch();
            }}
          >
            Retry
          </Button>
        </div>
      ) : null}
      {merge.error ? (
        <p role="alert" className="text-sm text-destructive">
          {merge.error instanceof ApiError
            ? merge.error.message
            : "Paper Trail could not reach the server. Try again."}
        </p>
      ) : null}
      <div className="flex flex-col gap-2 sm:flex-row-reverse">
        <Button
          type="button"
          size="lg"
          variant="destructive"
          disabled={merge.isPending || !preview.isSuccess}
          onClick={submit}
        >
          Merge
        </Button>
        <Button
          type="button"
          size="lg"
          variant="outline"
          disabled={merge.isPending}
          onClick={onBack}
        >
          Back
        </Button>
      </div>
    </div>
  );
}
