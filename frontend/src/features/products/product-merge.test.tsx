import { QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  renderHook,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { useMonthSummary } from "@/features/home/api";
import {
  productKeys,
  useMergeProduct,
  useProductList,
  useProductsToCheck,
} from "@/features/products/api";
import { receiptKeys, useReceipt } from "@/features/receipts/api";
import { useSearchResults } from "@/features/search/api";
import {
  product,
  productPurchase,
  productSummary,
  type ProductOut,
  type ProductPurchaseOut,
  type ProductSummaryOut,
} from "@/test/fixtures/products";
import { lineItem, receiptDetail } from "@/test/fixtures/receipts";
import { monthSummary, searchResults } from "@/test/fixtures/summary";
import type { ProblemDetail } from "@/test/msw/handlers";
import { server } from "@/test/msw/server";
import { createTestQueryClient, renderApp } from "@/test/render";
import { textOf } from "@/test/text";

const S = product({ id: 5, canonical_name: "RED PEPPERS 6PK" });
const K = productSummary({
  id: 9,
  canonical_name: "Bell peppers",
  brand: "Sunset",
  size_value: null,
  size_unit: null,
  times_bought: 1,
  last_purchase: {
    purchased_on: "2026-11-01",
    store_name: "METRO #77",
    paid: { amount_cents: 499, currency: "CAD" },
  },
});
const NAMED = productSummary({
  id: 11,
  canonical_name: "Red pepper strips",
  brand: null,
  size_value: null,
  size_unit: null,
  times_bought: 3,
});
const FOUND = productSummary({ id: 12, canonical_name: "Peppers, mixed" });

const costco: ProductPurchaseOut[] = [
  productPurchase({
    receipt_id: 2,
    purchased_on: "2026-10-15",
    paid: { amount_cents: 649, currency: "CAD" },
  }),
  productPurchase({
    receipt_id: 1,
    purchased_on: "2026-10-01",
    paid: { amount_cents: 599, currency: "CAD" },
  }),
  productPurchase({ receipt_id: 3, purchased_on: null }),
];
const metro = productPurchase({
  receipt_id: 4,
  purchased_on: "2026-11-01",
  store_name: "METRO #77",
  paid: { amount_cents: 499, currency: "CAD" },
});

function problem(status: number, detail: string | null): ProblemDetail {
  return {
    type: "about:blank",
    title: "Problem",
    status,
    detail,
    instance: null,
    errors: null,
  };
}

function problemResponse(status: number, detail: string | null) {
  return HttpResponse.json(problem(status, detail), {
    status,
    headers: { "content-type": "application/problem+json" },
  });
}

interface Served {
  merges: unknown[];
  reads: string[];
}

/** S's page with its three Costco purchases, the suggestions and K; K after a merge holds all
 * four. Returns the merge requests' bodies and the GETs of S. */
function serve({
  candidates = [K, NAMED, productSummary({ ...S })],
  merge = () =>
    HttpResponse.json(product({ id: 9, canonical_name: "Bell peppers" })),
  keptAs = "Bell peppers",
  preview = () => HttpResponse.json(product({ id: 9, canonical_name: keptAs })),
}: {
  candidates?: ProductSummaryOut[] | (() => Response);
  merge?: () => Response | Promise<Response>;
  keptAs?: string;
  preview?: () => Response | Promise<Response>;
} = {}): Served {
  const served: Served = { merges: [], reads: [] };
  const merged: Record<number, ProductOut> = {
    5: S,
    9: product({ id: 9, canonical_name: "Bell peppers" }),
  };
  server.use(
    http.get("/api/products/:productId", ({ params }) => {
      const id = Number(params.productId);
      if (id === 5) served.reads.push("/api/products/5");
      const found = merged[id];
      return found ? HttpResponse.json(found) : problemResponse(404, null);
    }),
    http.get("/api/products/:productId/purchases", ({ params }) => {
      const id = Number(params.productId);
      if (id === 5) served.reads.push("/api/products/5/purchases");
      return HttpResponse.json(id === 5 ? costco : [metro, ...costco]);
    }),
    http.get("/api/products/:productId/merge-preview", () => preview()),
    http.get("/api/products/:productId/merge-candidates", () =>
      typeof candidates === "function"
        ? candidates()
        : HttpResponse.json(candidates),
    ),
    http.get("/api/search", ({ request }) => {
      const q = new URL(request.url).searchParams.get("q") ?? "";
      return HttpResponse.json(
        searchResults({
          products: q.startsWith("pepper")
            ? [FOUND, productSummary({ ...S })]
            : [],
        }),
      );
    }),
    http.post("/api/products/:productId/merge", async ({ request }) => {
      served.merges.push(await request.json());
      return merge();
    }),
  );
  return served;
}

async function openPicker() {
  const user = userEvent.setup();
  const app = renderApp("/products/5");
  await screen.findByRole("heading", { level: 1, name: "RED PEPPERS 6PK" });
  await screen.findByRole("heading", { level: 2, name: /^Purchases/ });
  await user.click(screen.getByRole("button", { name: "Same product as…" }));
  return { user, ...app };
}

function enclosing(element: HTMLElement, selector: string): HTMLElement {
  const found = element.closest<HTMLElement>(selector);
  if (!found) throw new Error(`expected a ${selector} around the element`);
  return found;
}

function choices(title: string): string[] {
  return within(
    enclosing(screen.getByRole("heading", { name: title }), "section"),
  )
    .getAllByRole("button")
    .map((row) => textOf(row));
}

/** The Merge button, once the preview of the kept product has loaded. */
async function mergeReady(): Promise<HTMLElement> {
  const merge = await screen.findByRole("button", { name: "Merge" });
  await waitFor(() => {
    expect(merge).toBeEnabled();
  });
  return merge;
}

async function pickKept() {
  const opened = await openPicker();
  await opened.user.click(
    await screen.findByRole("button", { name: /^Bell peppers/ }),
  );
  await mergeReady();
  return opened;
}

describe("Same product as…", () => {
  it("offers the suggestions first, best first, never the open product, with Edit off", async () => {
    serve();

    const { user } = await openPicker();

    expect(
      await screen.findByRole("heading", { name: "Suggested" }),
    ).toBeInTheDocument();
    expect(choices("Suggested")).toEqual([
      "Bell peppersSunset · bought 1× · last at Metro",
      "Red pepper stripsbought 3× · last at Costco",
    ]);
    expect(screen.getByLabelText("Same product as…")).toHaveValue("");
    expect(screen.getByRole("button", { name: "Edit" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Edit" })).toBeEnabled();
  });

  it("searches instead of suggesting, and suggests again once the search is cleared", async () => {
    serve();
    const { user } = await openPicker();
    await screen.findByRole("heading", { name: "Suggested" });
    const field = screen.getByLabelText("Same product as…");

    await user.type(field, "pepper");

    expect(
      await screen.findByRole("heading", { name: "Results" }),
    ).toBeInTheDocument();
    expect(choices("Results")).toHaveLength(1);
    expect(choices("Results")[0]).toMatch(/^Peppers, mixed/);

    await user.clear(field);
    expect(
      await screen.findByRole("heading", { name: "Suggested" }),
    ).toBeInTheDocument();

    await user.type(field, "zz");
    expect(
      await screen.findByText("No product matches “zz”."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Merge" })).toBeNull();
  });

  it.each([
    {
      name: "no suggestions",
      candidates: [] as ProductSummaryOut[],
      shown: "Search for the product this one is the same as.",
    },
    {
      name: "failed suggestions",
      candidates: () => problemResponse(500, null),
      shown: "Couldn't load suggestions. Search for the product instead.",
    },
  ])("with $name, the picker still searches", async ({ candidates, shown }) => {
    serve({ candidates });
    const { user } = await openPicker();

    expect(await screen.findByText(shown)).toBeInTheDocument();
    await user.type(screen.getByLabelText("Same product as…"), "pepper");
    expect(
      await screen.findByRole("heading", { name: "Results" }),
    ).toBeInTheDocument();
  });

  it("confirms which product is kept, what it is called after, and that only a backup undoes it, sending nothing until Merge", async () => {
    const served = serve({ keptAs: "Red peppers, 6 pack" });
    const { user } = await pickKept();

    const confirm = await screen.findByRole("heading", {
      level: 3,
      name: /^Merge/,
    });
    await screen.findByText(/kept as/);
    expect(
      [
        ...enclosing(confirm, "div").querySelectorAll("h3, dt, dd, p, button"),
      ].map(textOf),
    ).toEqual([
      "Merge “RED PEPPERS 6PK” into “Bell peppers”?",
      "Merged away",
      "RED PEPPERS 6PK · Kirkland Signature · 24 each · bought 3× · last at Costco",
      "Kept",
      "Bell peppers · Sunset · bought 1× · last at Metro",
      "3 purchases of “RED PEPPERS 6PK” move to “Bell peppers”, and “RED PEPPERS 6PK” is deleted. It is kept as “Red peppers, 6 pack”.",
      "This can only be undone by restoring a backup.",
      "Merge",
      "Back",
    ]);

    await user.click(screen.getByRole("button", { name: "Back" }));
    expect(
      screen.getByRole("heading", { name: "Suggested" }),
    ).toBeInTheDocument();
    await user.type(screen.getByLabelText("Same product as…"), "pepper");
    await user.click(
      await screen.findByRole("button", { name: /^Peppers, mixed/ }),
    );
    await user.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByLabelText("Same product as…")).toHaveValue("pepper");

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByLabelText("Same product as…")).toBeNull();
    expect(served.merges).toEqual([]);
  });

  it("merges once, lands on the kept product in place of this one, and charts both stores", async () => {
    let release: () => void = () => undefined;
    const served = serve({
      merge: async () => {
        await new Promise<void>((resolve) => {
          release = resolve;
        });
        return HttpResponse.json(
          product({ id: 9, canonical_name: "Bell peppers" }),
        );
      },
    });
    const user = userEvent.setup();
    const { router } = renderApp("/products");
    await router.navigate({
      to: "/products/$productId",
      params: { productId: "5" },
    });
    await screen.findByRole("heading", { level: 2, name: /^Purchases/ });
    await user.click(screen.getByRole("button", { name: "Same product as…" }));
    await user.click(
      await screen.findByRole("button", { name: /^Bell peppers/ }),
    );
    const merge = await mergeReady();
    served.reads.length = 0;

    await user.dblClick(merge);

    expect(merge).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Back" })).toBeDisabled();
    release();
    expect(await screen.findByText("Products merged")).toBeInTheDocument();
    await screen.findByRole("heading", { level: 1, name: "Bell peppers" });
    expect(router.state.location.pathname).toBe("/products/9");
    const purchases = await screen.findByRole("heading", {
      level: 2,
      name: /^Purchases/,
    });
    expect(textOf(purchases)).toBe("Purchases (4)");
    const prices = enclosing(
      await screen.findByRole("heading", { level: 2, name: "Prices" }),
      "section",
    );
    expect(
      [...within(prices).getByRole("img").querySelectorAll("[data-store]")].map(
        (line) => line.getAttribute("data-store"),
      ),
    ).toEqual(["Costco", "Metro"]);
    expect(served.merges).toEqual([{ into_id: 9 }]);
    expect(served.reads).toEqual([]);

    router.history.back();
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/products");
    });
  });

  it("keeps Same product as… disabled while an edit is saved, and Edit while a merge runs", async () => {
    let release: () => void = () => undefined;
    serve({
      merge: async () => {
        await new Promise<void>((resolve) => {
          release = resolve;
        });
        return problemResponse(
          409,
          "A receipt is being read. Try again in a moment.",
        );
      },
    });
    server.use(
      http.patch("/api/products/:productId", async () => {
        await new Promise<void>((resolve) => {
          release = resolve;
        });
        return HttpResponse.json(S);
      }),
    );
    const user = userEvent.setup();
    const { router } = renderApp("/products/5");
    await screen.findByRole("heading", { level: 2, name: /^Purchases/ });
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.type(screen.getByLabelText(/^brand/i), "x");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Same product as…" }),
      ).toBeDisabled();
    });
    release();
    await screen.findByText("Product saved");
    await act(() =>
      router.navigate({
        to: "/products/$productId",
        params: { productId: "5" },
      }),
    );

    await user.click(
      await screen.findByRole("button", { name: "Same product as…" }),
    );
    await user.click(
      await screen.findByRole("button", { name: /^Bell peppers/ }),
    );
    await user.click(await mergeReady());
    expect(screen.getByRole("button", { name: "Edit" })).toBeDisabled();
    release();
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it.each([
    {
      name: "a server error with its detail",
      respond: () => problemResponse(500, "Something broke on the server."),
      shown: "Something broke on the server.",
    },
    {
      name: "a network failure",
      respond: () => HttpResponse.error(),
      shown: "Paper Trail could not reach the server. Try again.",
    },
    {
      name: "a receipt being read",
      respond: () =>
        problemResponse(409, "A receipt is being read. Try again in a moment."),
      shown: "A receipt is being read. Try again in a moment.",
    },
    {
      name: "a refusal",
      respond: () =>
        problemResponse(422, "product 5 cannot be merged into itself"),
      shown: "product 5 cannot be merged into itself",
    },
  ])(
    "after $name the confirm stays open with the message, ready to try again",
    async ({ respond, shown }) => {
      const served = serve({ merge: respond });
      const { user } = await pickKept();

      await user.click(screen.getByRole("button", { name: "Merge" }));

      expect(await screen.findByRole("alert")).toHaveTextContent(shown);
      expect(
        screen.getByRole("heading", { level: 3, name: /into “Bell peppers”/ }),
      ).toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: "Merge" }));
      await waitFor(() => {
        expect(served.merges).toHaveLength(2);
      });
    },
  );

  it("keeps Merge off while the kept product's name is being checked", async () => {
    let release: () => void = () => undefined;
    const served = serve({
      preview: async () => {
        await new Promise<void>((resolve) => {
          release = resolve;
        });
        return HttpResponse.json(
          product({ id: 9, canonical_name: "Bell peppers" }),
        );
      },
    });
    const { user } = await openPicker();
    await user.click(
      await screen.findByRole("button", { name: /^Bell peppers/ }),
    );

    expect(
      await screen.findByText("Checking what the kept product will be called…"),
    ).toBeInTheDocument();
    const merge = screen.getByRole("button", { name: "Merge" });
    expect(merge).toBeDisabled();
    await user.click(merge);
    expect(served.merges).toEqual([]);

    release();
    await waitFor(() => {
      expect(merge).toBeEnabled();
    });
    expect(screen.queryByText(/Checking what/)).toBeNull();
  });

  it("keeps Merge off after the check fails, until Retry succeeds", async () => {
    let previews = 0;
    const served = serve({
      preview: () => {
        previews += 1;
        return previews === 1
          ? problemResponse(500, null)
          : HttpResponse.json(
              product({ id: 9, canonical_name: "Bell peppers" }),
            );
      },
    });
    const { user } = await openPicker();
    await user.click(
      await screen.findByRole("button", { name: /^Bell peppers/ }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn't check what the kept product will be called.",
    );
    expect(screen.getByRole("button", { name: "Merge" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Retry" }));

    await mergeReady();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(served.merges).toEqual([]);
  });

  it("closes the confirm and reloads the products when one of them no longer exists", async () => {
    const served = serve({
      merge: () => problemResponse(404, "product 9 does not exist"),
    });
    const { user } = await pickKept();
    served.reads.length = 0;

    await user.click(screen.getByRole("button", { name: "Merge" }));

    expect(
      await screen.findByText(
        "One of these products no longer exists. Pick the product again.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Merge" })).toBeNull();
    await waitFor(() => {
      expect(served.reads).toContain("/api/products/5");
    });
  });

  it("forgets the confirm when the person leaves the page", async () => {
    const served = serve();
    const { router } = await pickKept();
    await screen.findByRole("button", { name: "Merge" });

    await act(() => router.navigate({ to: "/products" }));
    await act(() =>
      router.navigate({
        to: "/products/$productId",
        params: { productId: "5" },
      }),
    );

    await screen.findByRole("heading", { level: 1, name: "RED PEPPERS 6PK" });
    expect(screen.queryByLabelText("Same product as…")).toBeNull();
    expect(screen.queryByRole("button", { name: "Merge" })).toBeNull();
    expect(served.merges).toEqual([]);
  });
});

describe("useMergeProduct", () => {
  it("refreshes every view that showed the merged product and drops its own queries", async () => {
    const reads: string[] = [];
    const record = (request: Request) => {
      reads.push(new URL(request.url).pathname);
    };
    let receiptReads = 0;
    server.use(
      http.get("/api/products", ({ request }) => {
        record(request);
        return HttpResponse.json([]);
      }),
      http.get("/api/products/to-check", ({ request }) => {
        record(request);
        return HttpResponse.json([]);
      }),
      http.get("/api/summary", ({ request }) => {
        record(request);
        return HttpResponse.json(monthSummary());
      }),
      http.get("/api/search", ({ request }) => {
        record(request);
        return HttpResponse.json(searchResults());
      }),
      http.get("/api/receipts/:receiptId", async ({ request }) => {
        record(request);
        receiptReads += 1;
        await delay(0);
        return HttpResponse.json(
          receiptDetail({
            id: 3,
            items: [
              lineItem({
                product_id: receiptReads === 1 ? 5 : 9,
                product_name:
                  receiptReads === 1 ? "RED PEPPERS 6PK" : "Bell peppers",
              }),
            ],
          }),
        );
      }),
      http.get("/api/products/:productId", ({ request }) => {
        record(request);
        return HttpResponse.json(S);
      }),
      http.post("/api/products/:productId/merge", () =>
        HttpResponse.json(product({ id: 9 })),
      ),
    );
    const queryClient = createTestQueryClient();
    queryClient.setQueryData(productKeys.detail(5), S);
    queryClient.setQueryData(productKeys.purchases(5), costco);
    const { result } = renderHook(
      () => ({
        list: useProductList(),
        toCheck: useProductsToCheck(),
        summary: useMonthSummary("2026-11"),
        search: useSearchResults("pepper"),
        receipt: useReceipt(3),
        merge: useMergeProduct(5),
      }),
      {
        wrapper: ({ children }: { children: ReactNode }) => (
          <QueryClientProvider client={queryClient}>
            {children}
          </QueryClientProvider>
        ),
      },
    );
    await waitFor(() => {
      expect(result.current.receipt.data?.items[0]?.product_id).toBe(5);
      expect(result.current.list.isSuccess).toBe(true);
      expect(result.current.toCheck.isSuccess).toBe(true);
      expect(result.current.summary.isSuccess).toBe(true);
      expect(result.current.search.isSuccess).toBe(true);
    });
    reads.length = 0;

    await act(async () => {
      await result.current.merge.mutateAsync(9);
    });

    await waitFor(() => {
      expect([...reads].sort()).toEqual([
        "/api/products",
        "/api/products/to-check",
        "/api/receipts/3",
        "/api/search",
        "/api/summary",
      ]);
    });
    await waitFor(() => {
      expect(result.current.receipt.data?.items[0]).toMatchObject({
        product_id: 9,
        product_name: "Bell peppers",
      });
    });
    expect(queryClient.getQueryData(productKeys.detail(5))).toBeUndefined();
    expect(queryClient.getQueryData(productKeys.purchases(5))).toBeUndefined();
    expect(queryClient.getQueryData(receiptKeys.detail(3))).toBeDefined();
  });
});
