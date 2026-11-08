import type { QueryClient } from "@tanstack/react-query";
import {
  createRootRouteWithContext,
  createRoute,
  createRouter,
  redirect,
  type RouterHistory,
} from "@tanstack/react-router";

import { NotFoundPage } from "@/routes/not-found-page";
import { ReceiptDetailPage } from "@/routes/receipt-detail-page";
import { ReceiptsPage } from "@/routes/receipts-page";
import { ReviewPage } from "@/routes/review-page";
import { RootLayout } from "@/routes/root-layout";
import { ScanPage } from "@/routes/scan-page";
import { SearchPage } from "@/routes/search-page";

export interface RouterContext {
  queryClient: QueryClient;
}

const rootRoute = createRootRouteWithContext<RouterContext>()({
  component: RootLayout,
  notFoundComponent: NotFoundPage,
});

const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  beforeLoad: () => {
    // eslint-disable-next-line @typescript-eslint/only-throw-error -- TanStack Router's redirect protocol
    throw redirect({ to: "/scan" });
  },
});

const pageRoutes = [
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
    path: "/search",
    component: SearchPage,
  }),
  createRoute({
    getParentRoute: () => rootRoute,
    path: "/review",
    component: ReviewPage,
  }),
] as const;

const routeTree = rootRoute.addChildren([indexRoute, ...pageRoutes]);

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
