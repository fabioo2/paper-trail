import type { QueryClient } from "@tanstack/react-query";
import {
  createRootRouteWithContext,
  createRoute,
  createRouter,
  redirect,
  type RouterHistory,
} from "@tanstack/react-router";

import { HomePage } from "@/routes/home-page";
import { NotFoundPage } from "@/routes/not-found-page";
import { ProductCheckPage } from "@/routes/product-check-page";
import { ProductDetailPage } from "@/routes/product-detail-page";
import { ProductsPage } from "@/routes/products-page";
import { ReceiptDetailPage } from "@/routes/receipt-detail-page";
import { ReceiptsPage } from "@/routes/receipts-page";
import { ReviewDetailPage } from "@/routes/review-detail-page";
import { ReviewPage } from "@/routes/review-page";
import { RootLayout } from "@/routes/root-layout";
import { ScanPage } from "@/routes/scan-page";
import { SearchPage } from "@/routes/search-page";

export interface RouterContext {
  queryClient: QueryClient;
}

/**
 * The products list's filter chip (one category, one store, or the products to check) and what
 * the header search typed.
 */
export interface ProductsSearch {
  /** Open the list on the products whose details were guessed. */
  needsALook?: boolean;
  category?: string;
  /** A store as displayed (`displayStoreName`). */
  store?: string;
  q?: string;
}

function productsChip(search: Record<string, unknown>): ProductsSearch {
  if (search.needsALook === true) return { needsALook: true };
  if (typeof search.category === "string") {
    return { category: search.category };
  }
  if (typeof search.store === "string") return { store: search.store };
  return {};
}

function productsSearch(search: Record<string, unknown>): ProductsSearch {
  const chip = productsChip(search);
  return typeof search.q === "string" && search.q !== ""
    ? { ...chip, q: search.q }
    : chip;
}

export interface ResultsSearch {
  q?: string;
}

const rootRoute = createRootRouteWithContext<RouterContext>()({
  component: RootLayout,
  notFoundComponent: NotFoundPage,
});

const pageRoutes = [
  createRoute({
    getParentRoute: () => rootRoute,
    path: "/",
    component: HomePage,
  }),
  createRoute({
    getParentRoute: () => rootRoute,
    path: "/scan",
    component: ScanPage,
  }),
  createRoute({
    getParentRoute: () => rootRoute,
    path: "/receipts",
    component: ReceiptsPage,
  }),
  createRoute({
    getParentRoute: () => rootRoute,
    path: "/receipts/$receiptId",
    component: ReceiptDetailPage,
  }),
  createRoute({
    getParentRoute: () => rootRoute,
    path: "/products",
    component: ProductsPage,
    validateSearch: productsSearch,
  }),
  createRoute({
    getParentRoute: () => rootRoute,
    path: "/products/check",
    component: ProductCheckPage,
  }),
  createRoute({
    getParentRoute: () => rootRoute,
    path: "/products/$productId",
    component: ProductDetailPage,
  }),
  createRoute({
    getParentRoute: () => rootRoute,
    path: "/search",
    component: SearchPage,
    validateSearch: (search: Record<string, unknown>): ResultsSearch =>
      typeof search.q === "string" ? { q: search.q } : {},
    beforeLoad: ({ search }) => {
      if (!search.q?.trim()) {
        // eslint-disable-next-line @typescript-eslint/only-throw-error -- TanStack Router's redirect protocol
        throw redirect({ to: "/" });
      }
    },
  }),
  createRoute({
    getParentRoute: () => rootRoute,
    path: "/review",
    component: ReviewPage,
  }),
  createRoute({
    getParentRoute: () => rootRoute,
    path: "/review/$reviewId",
    component: ReviewDetailPage,
  }),
] as const;

const routeTree = rootRoute.addChildren([...pageRoutes]);

interface CreateAppRouterOptions {
  queryClient: QueryClient;
  /** Browser history by default; tests pass a memory history. */
  history?: RouterHistory;
}

export function createAppRouter({
  queryClient,
  history,
}: CreateAppRouterOptions) {
  return createRouter({
    routeTree,
    context: { queryClient },
    ...(history ? { history } : {}),
    defaultPreload: "intent",
    scrollRestoration: true,
  });
}

export type AppRouter = ReturnType<typeof createAppRouter>;

declare module "@tanstack/react-router" {
  interface Register {
    router: AppRouter;
  }
}
