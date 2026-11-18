import { Link } from "@tanstack/react-router";
import {
  ClipboardCheck,
  House,
  type LucideIcon,
  Package,
  ReceiptText,
  ScanLine,
} from "lucide-react";

import { useReviewList } from "@/features/reviews/api";
import { cn } from "@/lib/utils";

interface Tab {
  to: "/" | "/receipts" | "/scan" | "/products" | "/review";
  label: string;
  icon: LucideIcon;
}

const TABS: readonly Tab[] = [
  { to: "/", label: "Home", icon: House },
  { to: "/receipts", label: "Receipts", icon: ReceiptText },
  { to: "/scan", label: "Scan", icon: ScanLine },
  { to: "/products", label: "Products", icon: Package },
  { to: "/review", label: "Review", icon: ClipboardCheck },
];

/** The app's primary navigation, fixed to the bottom of the screen above the home indicator. */
export function BottomTabBar() {
  const reviews = useReviewList();
  const waiting = reviews.data?.length ?? 0;
  return (
    <nav
      aria-label="Primary"
      className="fixed inset-x-0 bottom-0 z-10 border-t bg-background/95 pb-safe-bottom backdrop-blur"
    >
      <ul className="mx-auto grid h-tab-bar max-w-2xl grid-cols-5">
        {TABS.map(({ to, label, icon: Icon }) => {
          const raised = to === "/scan";
          return (
            <li key={to} className="flex">
              <Link
                to={to}
                activeOptions={{ exact: to === "/" }}
                className="group flex min-h-11 min-w-11 flex-1 flex-col items-center justify-center gap-1 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none aria-[current=page]:text-primary"
              >
                <span
                  className={cn(
                    "relative",
                    raised &&
                      "-mt-6 flex size-12 items-center justify-center rounded-full bg-primary text-primary-foreground shadow-md ring-4 ring-background group-hover:bg-primary/90",
                  )}
                >
                  <Icon
                    aria-hidden="true"
                    className={raised ? "size-6" : "size-5"}
                  />
                  {to === "/review" && waiting > 0 ? (
                    <span
                      aria-hidden="true"
                      className="absolute -top-1.5 -right-2.5 min-w-4 rounded-full bg-primary px-1 text-[10px] leading-4 text-primary-foreground tabular-nums"
                    >
                      {waiting}
                    </span>
                  ) : null}
                </span>
                {label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
