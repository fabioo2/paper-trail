import { Link } from "@tanstack/react-router";
import { CloudOff, LoaderCircle, ScanLine } from "lucide-react";
import { type ReactNode, useEffect } from "react";
import { toast } from "sonner";

import { EmptyState } from "@/components/empty-state";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  type MonthSummaryOut,
  monthOf,
  type StoreSpendOut,
  useMonthSummary,
  type WeekSpendOut,
} from "@/features/home/api";
import {
  formatMonthDay,
  formatMonthName,
  formatWeekday,
} from "@/features/receipts/dates";
import { formatMoney, type MoneyOut } from "@/features/receipts/money";
import { displayStoreName } from "@/features/receipts/store-name";
import { cn } from "@/lib/utils";

const LOAD_ERROR_TOAST = "summary-load-error";

/** Store colours in the split bar, in order; later stores reuse them. */
const STORE_COLORS = [
  "var(--primary)",
  "var(--cat-dairy-eggs)",
  "var(--cat-snacks-candy)",
  "var(--cat-beverages)",
  "var(--cat-produce)",
  "var(--cat-alcohol)",
];

function plural(count: number, one: string, many: string): string {
  return `${String(count)} ${count === 1 ? one : many}`;
}

function sums(amounts: MoneyOut[]): string {
  return amounts.map((amount) => formatMoney(amount)).join(" + ");
}

/** Home: this month's spending at a glance, what needs the person, and the latest receipts. */
export function HomeDashboard() {
  const today = new Date();
  const summary = useMonthSummary(monthOf(today));
  const { isError, refetch } = summary;

  useEffect(() => {
    if (!isError) {
      toast.dismiss(LOAD_ERROR_TOAST);
      return;
    }
    toast.error("Couldn't load your month", {
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

  if (summary.isPending) return <HomeSkeleton />;
  if (summary.isError) {
    return (
      <EmptyState
        icon={CloudOff}
        title="Home is unavailable"
        description="Paper Trail could not reach the server. Retry from the notice once you are back online."
      />
    );
  }
  const data = summary.data;
  if (data.recent.length === 0 && data.processing === 0) {
    return (
      <EmptyState
        icon={ScanLine}
        title="Scan your first receipt"
        description="Take a photo of a receipt and Paper Trail reads it for you: where your money went, what you saved and what you buy most."
      >
        <Button asChild className="mt-2 min-h-11">
          <Link to="/scan">Scan a receipt</Link>
        </Button>
      </EmptyState>
    );
  }

  return (
    <section
      aria-labelledby="home-heading"
      className="flex flex-col gap-3 px-4 py-4"
    >
      <h1 id="home-heading" className="sr-only">
        Home
      </h1>
      {data.processing > 0 ? <ReadingCard count={data.processing} /> : null}
      <MonthCard summary={data} today={today} />
      {data.stores.length > 0 ? <StoresCard stores={data.stores} /> : null}
      <NeedsYouCard summary={data} />
      {data.recent.length > 0 ? <RecentCard summary={data} /> : null}
    </section>
  );
}

function HomeCard({
  title,
  action,
  children,
  className = "bg-card",
}: {
  title: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      aria-label={title}
      className={cn("flex flex-col gap-3 rounded-xl border p-4", className)}
    >
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="font-semibold">{title}</h2>
        {action}
      </div>
      {children}
    </section>
  );
}

function CardLink({ to, children }: { to: "/receipts"; children: string }) {
  return (
    <Link
      to={to}
      className="text-sm font-medium text-primary underline-offset-4 hover:underline"
    >
      {children}
    </Link>
  );
}

function ReadingCard({ count }: { count: number }) {
  return (
    <div
      role="status"
      className="flex items-center gap-3 rounded-xl border bg-accent p-4 text-accent-foreground"
    >
      <LoaderCircle aria-hidden="true" className="size-5 animate-spin" />
      <div>
        <p className="font-medium">
          {count === 1
            ? "Reading your receipt…"
            : `Reading ${String(count)} receipts…`}
        </p>
        <p className="text-sm">You can keep using the app.</p>
      </div>
    </div>
  );
}

function MonthCard({
  summary,
  today,
}: {
  summary: MonthSummaryOut;
  today: Date;
}) {
  const saved = summary.saved.filter((amount) => amount.amount_cents > 0);
  return (
    <HomeCard
      title={`${formatMonthName(`${summary.month}-01`)} so far`}
      action={<CardLink to="/receipts">All receipts</CardLink>}
    >
      <p className="text-3xl font-semibold tracking-tight tabular-nums">
        {summary.spent.length > 0
          ? sums(summary.spent)
          : formatMoney({ amount_cents: 0, currency: "CAD" })}
      </p>
      <p className="text-sm text-muted-foreground">
        {[
          plural(summary.trips, "trip", "trips"),
          saved.length > 0 ? `${sums(saved)} saved on deals` : "",
        ]
          .filter(Boolean)
          .join(" · ")}
      </p>
      <WeekBars
        weeks={summary.weeks}
        currency={summary.spent[0]?.currency}
        today={today}
      />
    </HomeCard>
  );
}

/** One bar per week, scaled to the busiest week in the month's main currency. */
function WeekBars({
  weeks,
  currency,
  today,
}: {
  weeks: WeekSpendOut[];
  currency: string | undefined;
  today: Date;
}) {
  const todayIso = `${monthOf(today)}-${String(today.getDate()).padStart(2, "0")}`;
  const amountOf = (week: WeekSpendOut) =>
    week.spent.find((amount) => amount.currency === currency)?.amount_cents ??
    0;
  const busiest = Math.max(1, ...weeks.map(amountOf));
  return (
    <ul className="grid grid-cols-4 gap-2">
      {weeks.map((week) => {
        const future = week.start > todayIso;
        return (
          <li key={week.start} className="flex flex-col gap-1">
            <div aria-hidden="true" className="flex h-16 items-end">
              <div
                data-testid="week-bar"
                className={cn(
                  "w-full rounded-sm",
                  future ? "bg-muted" : "bg-primary",
                )}
                style={{
                  height: `${String(Math.max(4, (amountOf(week) / busiest) * 100))}%`,
                }}
              />
            </div>
            <span className="text-xs text-muted-foreground">
              {formatMonthDay(week.start)}–{Number(week.end.slice(8, 10))}
            </span>
            <span className="text-xs font-medium tabular-nums">
              {future ? "" : week.spent.length > 0 ? sums(week.spent) : "—"}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

function StoresCard({ stores }: { stores: StoreSpendOut[] }) {
  const currency = stores[0]?.spent.currency;
  const colored = stores.map((store, index) => ({
    store,
    color: STORE_COLORS[index % STORE_COLORS.length],
  }));
  return (
    <HomeCard title="By store">
      <div aria-hidden="true" className="flex h-3 overflow-hidden rounded-full">
        {colored
          .filter(({ store }) => store.spent.currency === currency)
          .map(({ store, color }, index) => (
            <span
              key={index}
              style={{ flexGrow: store.spent.amount_cents, background: color }}
            />
          ))}
      </div>
      <ul className="flex flex-col gap-1.5 text-sm">
        {colored.map(({ store, color }, index) => (
          <li key={index} className="flex items-center justify-between gap-3">
            <span className="flex min-w-0 items-center gap-2">
              <span
                aria-hidden="true"
                className="inline-block size-2 shrink-0 rounded-full"
                style={{ background: color }}
              />
              <span className="truncate">
                {displayStoreName(store.store_name)}
              </span>
            </span>
            <span className="tabular-nums">{formatMoney(store.spent)}</span>
          </li>
        ))}
      </ul>
    </HomeCard>
  );
}

function NeedsYouCard({ summary }: { summary: MonthSummaryOut }) {
  const { open_reviews: reviews, unconfirmed_products: products } = summary;
  if (reviews === 0 && products === 0) {
    return (
      <p className="px-1 text-sm text-muted-foreground">
        All caught up: no receipts or products need you.
      </p>
    );
  }
  return (
    <HomeCard title="Needs you" className="border-warning bg-warning/10">
      <ul className="flex flex-col gap-2 text-sm">
        {reviews > 0 ? (
          <li>
            <Link
              to="/review"
              className="font-medium underline-offset-4 hover:underline"
            >
              {plural(reviews, "receipt", "receipts")} to review
            </Link>
          </li>
        ) : null}
        {products > 0 ? (
          <li>
            <Link
              to="/products/check"
              className="font-medium underline-offset-4 hover:underline"
            >
              {plural(products, "product", "products")} to check
            </Link>
          </li>
        ) : null}
      </ul>
    </HomeCard>
  );
}

function RecentCard({ summary }: { summary: MonthSummaryOut }) {
  return (
    <HomeCard
      title="Recent"
      action={<CardLink to="/receipts">See all</CardLink>}
    >
      <ul className="-mx-4 -mb-4 divide-y border-t">
        {summary.recent.map((receipt) => (
          <li key={receipt.id}>
            <Link
              to="/receipts/$receiptId"
              params={{ receiptId: String(receipt.id) }}
              className="flex min-h-14 items-center justify-between gap-3 px-4 py-2 hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
            >
              <span className="min-w-0">
                <span className="block truncate font-medium">
                  {receipt.status === "processing"
                    ? "Reading…"
                    : displayStoreName(receipt.store_name)}
                </span>
                <span className="block text-sm text-muted-foreground">
                  {receipt.purchased_on === null
                    ? "No date"
                    : `${formatWeekday(receipt.purchased_on)}, ${formatMonthDay(receipt.purchased_on)}`}
                </span>
              </span>
              <span className="shrink-0 font-medium tabular-nums">
                {receipt.total === null ? "—" : formatMoney(receipt.total)}
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </HomeCard>
  );
}

function HomeSkeleton() {
  return (
    <div
      aria-busy="true"
      aria-label="Loading home"
      className="flex flex-col gap-3 px-4 py-4"
    >
      <Skeleton className="h-48 w-full rounded-xl" />
      <Skeleton className="h-28 w-full rounded-xl" />
      <Skeleton className="h-36 w-full rounded-xl" />
    </div>
  );
}
