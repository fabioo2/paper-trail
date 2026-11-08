import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import type { QueryClient } from "@tanstack/react-query";
import { render, type RenderResult } from "@testing-library/react";
import type { ReactElement } from "react";

import { AppProviders } from "@/app/providers";
import { createQueryClient } from "@/app/query-client";
import { type AppRouter, createAppRouter } from "@/app/router";

/** The production query client, minus retries so failure states render immediately. */
export function createTestQueryClient(): QueryClient {
  const queryClient = createQueryClient();
  const defaults = queryClient.getDefaultOptions();
  queryClient.setDefaultOptions({
    ...defaults,
    queries: { ...defaults.queries, retry: false },
  });
  return queryClient;
}

export type AppRenderResult = RenderResult & {
  router: AppRouter;
  queryClient: QueryClient;
};

/** Render the whole app (providers + router) at `path`, as main.tsx does in the browser. */
export function renderApp(path = "/"): AppRenderResult {
  const queryClient = createTestQueryClient();
  const router = createAppRouter({
    queryClient,
    history: createMemoryHistory({ initialEntries: [path] }),
  });
  const result = render(
    <AppProviders queryClient={queryClient}>
      <RouterProvider router={router} />
    </AppProviders>,
  );
  return { ...result, router, queryClient };
}

/** Render a single component inside the app providers (query client, theme, toaster). */
export function renderWithProviders(ui: ReactElement): RenderResult {
  return render(
    <AppProviders queryClient={createTestQueryClient()}>{ui}</AppProviders>,
  );
}
