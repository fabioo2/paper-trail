import { Link } from "@tanstack/react-router";
import { CloudOff, Search, SearchX } from "lucide-react";
import type { ReactNode } from "react";

import { EmptyState } from "@/components/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { type CategoryOut, useCategories } from "@/features/products/api";
import { formatMonthDay } from "@/features/receipts/dates";
import { formatMoney } from "@/features/receipts/money";
import { displayStoreName } from "@/features/receipts/store-name";
import {
  type ReceiptMatchOut,
  SEARCH_MIN_CHARS,
  type SearchOut,
  useSearchResults,
} from "@/features/search/api";

const WORD = /[\p{L}\p{N}]+/gu;
const ROW =
  "flex min-h-14 items-center justify-between gap-3 px-4 py-2 hover:bg-accent focus-visible:bg-accent focus-visible:outline-none";

/**
 * `text` with every word that starts like a word of `query` marked, ignoring case: the server
 * matches words by prefix, so the marks show why a row was found.
 */
export function Highlight({ text, query }: { text: string; query: string }) {
  const words = (query.toLowerCase().match(WORD) ?? []).sort(
    (a, b) => b.length - a.length,
  );
  if (words.length === 0) return text;
  const escaped = words.map((word) =>
    word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"),
  );
  const pattern = new RegExp(
    `(?<![\\p{L}\\p{N}])(${escaped.join("|")})`,
    "giu",
  );
  const parts: ReactNode[] = [];
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    parts.push(text.slice(last, match.index));
    parts.push(
      <mark
        key={match.index}
        className="rounded-sm bg-warning/30 text-foreground"
      >
        {match[0]}
      </mark>,
    );
    last = match.index + match[0].length;
  }
  parts.push(text.slice(last));
  return <>{parts}</>;
}

/** The header search's results: products, then receipts, each with its matches marked. */
export function SearchResults({ query }: { query: string }) {
  const results = useSearchResults(query);
  const categories = useCategories();

  if (query.trim().length < SEARCH_MIN_CHARS) {
    return (
      <EmptyState
        icon={Search}
        title="Search your purchases"
        description="Type at least two letters to find products by name, brand or category, and receipts by store or anything printed on them."
      />
    );
  }
  if (results.isPending) return <SearchSkeleton />;
  if (results.isError) {
    return (
      <EmptyState
        icon={CloudOff}
        title="Search is unavailable"
        description="Paper Trail could not reach the server. Try again once you are back online."
      />
    );
  }
  const { products, receipts }: SearchOut = results.data;
  if (products.length === 0 && receipts.length === 0) {
    return (
      <EmptyState
        icon={SearchX}
        title="Nothing found"
        description={`No product or receipt matches “${query.trim()}”. Try a shorter word or another spelling.`}
      />
    );
  }

  return (
    <section
      aria-labelledby="search-heading"
      className="flex flex-col gap-4 px-4 py-4"
    >
      <h1 id="search-heading" className="sr-only">
        Search results
      </h1>
      {products.length > 0 ? (
        <ResultGroup title="Products" count={products.length}>
          {products.map((product) => {
            const category = labelOf(product.category, categories.data);
            const last = product.last_purchase;
            return (
              <li key={product.id}>
                <Link
                  to="/products/$productId"
                  params={{ productId: String(product.id) }}
                  className={ROW}
                >
                  <ResultRow
                    title={
                      <Highlight text={product.canonical_name} query={query} />
                    }
                    details={
                      <>
                        {[product.brand, category]
                          .filter(Boolean)
                          .map((text) => (
                            <span key={text}>
                              <Highlight text={text ?? ""} query={query} />
                              {" · "}
                            </span>
                          ))}
                        bought {product.times_bought}×
                      </>
                    }
                    amount={formatMoney(last.paid)}
                    note={`Last bought at ${displayStoreName(last.store_name)}${last.purchased_on === null ? "" : `, ${formatMonthDay(last.purchased_on)}`}`}
                  />
                </Link>
              </li>
            );
          })}
        </ResultGroup>
      ) : null}
      {receipts.length > 0 ? (
        <ResultGroup title="Receipts" count={receipts.length}>
          {receipts.map((receipt) => (
            <li key={receipt.id}>
              <ReceiptResult receipt={receipt} query={query} />
            </li>
          ))}
        </ResultGroup>
      ) : null}
    </section>
  );
}

function labelOf(
  category: string | null,
  categories: CategoryOut[] | undefined,
): string | null {
  if (category === null) return null;
  return (
    categories?.find((option) => option.value === category)?.label ?? category
  );
}

function ResultGroup({
  title,
  count,
  children,
}: {
  title: string;
  count: number;
  children: ReactNode;
}) {
  return (
    <section aria-label={title}>
      <h2 className="mb-2 flex items-baseline justify-between px-1">
        <span className="font-semibold">{title}</span>
        <span className="text-sm text-muted-foreground tabular-nums">
          {count}
        </span>
      </h2>
      <ul className="divide-y rounded-xl border bg-card">{children}</ul>
    </section>
  );
}

function ReceiptResult({
  receipt,
  query,
}: {
  receipt: ReceiptMatchOut;
  query: string;
}) {
  const date =
    receipt.purchased_on === null
      ? "No date"
      : formatMonthDay(receipt.purchased_on);
  return (
    <Link
      to="/receipts/$receiptId"
      params={{ receiptId: String(receipt.id) }}
      className={ROW}
    >
      <ResultRow
        title={
          <Highlight
            text={displayStoreName(receipt.store_name)}
            query={query}
          />
        }
        details={
          <>
            {date}
            {receipt.lines.length > 0 ? " · " : ""}
            <Highlight text={receipt.lines.join(", ")} query={query} />
          </>
        }
        amount={receipt.total === null ? "—" : formatMoney(receipt.total)}
      />
    </Link>
  );
}

function ResultRow({
  title,
  details,
  amount,
  note,
}: {
  title: ReactNode;
  details: ReactNode;
  amount: string;
  note?: string;
}) {
  return (
    <>
      <div className="min-w-0">
        <p className="truncate font-medium">{title}</p>
        <p className="truncate text-sm text-muted-foreground">{details}</p>
        {note ? (
          <p className="truncate text-xs text-muted-foreground">{note}</p>
        ) : null}
      </div>
      <span className="shrink-0 font-medium tabular-nums">{amount}</span>
    </>
  );
}

function SearchSkeleton() {
  return (
    <div aria-busy="true" aria-label="Searching" className="px-4 py-4">
      <div className="divide-y rounded-xl border bg-card">
        {Array.from({ length: 4 }, (_, index) => (
          <div
            key={index}
            className="flex min-h-14 items-center justify-between gap-3 px-4 py-2"
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
