import { useLocation, useNavigate, useSearch } from "@tanstack/react-router";
import { Search } from "lucide-react";
import { useEffect, useState } from "react";

import { Input } from "@/components/ui/input";

/** Typing pauses this long before the results follow. */
export const SEARCH_DEBOUNCE_MS = 300;

/**
 * The search field of the top bar: what is typed opens the results at /search and keeps them in
 * step; clearing it there goes back Home. On Products it filters the list instead (`q`).
 * The field shows the query of the results on screen, so it clears when they are left and
 * comes back with them (the browser's Back).
 */
export function HeaderSearch() {
  const navigate = useNavigate();
  const pathname = useLocation({ select: (location) => location.pathname });
  const onResults = pathname === "/search";
  const onProducts = pathname === "/products";
  const { q = "" } = useSearch({ strict: false });
  const shown = onResults || onProducts ? q : "";
  const [value, setValue] = useState(shown);
  const [lastShown, setLastShown] = useState(shown);
  const [lastPath, setLastPath] = useState(pathname);
  // The query this field navigated to itself: typing may have gone on since.
  const [sent, setSent] = useState<string | null>(null);
  if (lastShown !== shown || lastPath !== pathname) {
    setLastShown(shown);
    setLastPath(pathname);
    if (shown !== sent) setValue(shown);
    setSent(null);
  }

  const text = value.trim();
  const settled = text === shown.trim();
  useEffect(() => {
    if (settled) return;
    const timer = setTimeout(() => {
      if (onProducts) {
        setSent(text);
        void navigate({
          to: "/products",
          search: (prev) => {
            const chip = { ...prev };
            delete chip.q;
            return text === "" ? chip : { ...chip, q: text };
          },
          replace: true,
        });
        return;
      }
      if (text === "") {
        void navigate({ to: "/" });
        return;
      }
      setSent(text);
      void navigate({ to: "/search", search: { q: text }, replace: onResults });
    }, SEARCH_DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [settled, text, onResults, onProducts, navigate]);

  return (
    <form
      role="search"
      className="relative min-w-0 flex-1"
      onSubmit={(event) => {
        event.preventDefault();
        if (text === "" || onProducts) return;
        setSent(text);
        void navigate({
          to: "/search",
          search: { q: text },
          replace: onResults,
        });
      }}
    >
      <Search
        aria-hidden="true"
        className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground"
      />
      <Input
        type="search"
        aria-label={
          onProducts ? "Search products" : "Search receipts and products"
        }
        placeholder={onProducts ? "Search products" : "Search"}
        value={value}
        onChange={(event) => {
          setValue(event.target.value);
        }}
        className="h-11 pl-8"
      />
    </form>
  );
}
