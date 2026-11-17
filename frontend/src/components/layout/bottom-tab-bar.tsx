import { Link } from "@tanstack/react-router";
import {
  ClipboardCheck,
  type LucideIcon,
  Package,
  ReceiptText,
  ScanLine,
  Search,
} from "lucide-react";

import { useReviewList } from "@/features/reviews/api";

interface Tab {
  to: "/scan" | "/receipts" | "/products" | "/search" | "/review";
  label: string;
  icon: LucideIcon;
}

const TABS: readonly Tab[] = [
  { to: "/scan", label: "Scan", icon: ScanLine },
  { to: "/receipts", label: "Receipts", icon: ReceiptText },
  { to: "/products", label: "Products", icon: Package },
  { to: "/search", label: "Search", icon: Search },
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
        {TABS.map(({ to, label, icon: Icon }) => (
          <li key={to} className="flex">
            <Link
              to={to}
              className="flex min-h-11 min-w-11 flex-1 flex-col items-center justify-center gap-1 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none aria-[current=page]:text-primary"
            >
              <span className="relative">
                <Icon aria-hidden="true" className="size-5" />
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
        ))}
      </ul>
    </nav>
  );
}
