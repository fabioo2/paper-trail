import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import {
  product,
  type ProductOut,
  productToCheck,
} from "@/test/fixtures/products";
import type { ProblemDetail } from "@/test/msw/handlers";
import { server } from "@/test/msw/server";
import { renderApp } from "@/test/render";
import { textOf } from "@/test/text";

const eggs = product({
  field_sources: { canonical_name: "llm", brand: "off", category: "llm" },
  size_value: null,
  size_unit: null,
});
const kiwi = product({
  id: 6,
  canonical_name: "Gold Kiwi",
  brand: "Zespri",
  category: "produce",
  tags: ["fruit"],
  field_sources: {
    canonical_name: "receipt_text",
    brand: "llm",
    size: "llm",
    tags: "llm",
  },
});
const milk = product({
  id: 7,
  canonical_name: "Natrel Milk",
  field_sources: { canonical_name: "llm" },
});

const PRINTED: Record<number, string> = {
  5: "KS ORG EGGS 24CT",
  6: "GOLD KIWI",
  7: "NATREL 2%",
};

const conflict: ProblemDetail = {
  type: "/problems/conflict",
  title: "Conflict",
  status: 409,
  detail: "product 5 changed since it was shown: category",
  instance: "/api/products/5/confirm",
  errors: null,
};

interface Backend {
  confirms: [number, unknown][];
  patches: [number, unknown][];
  stored: Map<number, ProductOut>;
}

type Field = "canonical_name" | "brand" | "size" | "category" | "tags";

function shown(item: ProductOut, field: Field): unknown {
  const values: Record<Field, unknown> = {
    canonical_name: item.canonical_name,
    brand: item.brand,
    size:
      item.size_value === null
        ? null
        : `${item.size_value} ${String(item.size_unit)}`,
    category: item.category,
    tags: item.tags.length > 0 ? item.tags : null,
  };
  return values[field];
}

function confirmed(sources: Record<string, string>): boolean {
  return ["person", "plu", "off", "upc"].includes(sources.canonical_name ?? "");
}

/** A stateful backend like the real one: confirm and PATCH refuse values that changed since
 * they were shown, PATCH writes and clears fields, and the to-check list leaves out confirmed
 * products. */
function serveQueue(...products: ProductOut[]): Backend {
  const stored = new Map(products.map((item) => [item.id, item]));
  const backend: Backend = { confirms: [], patches: [], stored };
  const find = (id: unknown): ProductOut => {
    const found = stored.get(Number(id));
    if (!found) throw new Error(`no product ${String(id)}`);
    return found;
  };
  const changed = (
    before: ProductOut,
    expected: Partial<Record<Field, unknown>> | null | undefined,
  ): boolean =>
    Object.entries(expected ?? {}).some(
      ([field, value]) =>
        JSON.stringify(shown(before, field as Field)) !== JSON.stringify(value),
    );
  const refused = () =>
    HttpResponse.json(conflict, {
      status: 409,
      headers: { "content-type": "application/problem+json" },
    });
  server.use(
    http.get("/api/products/to-check", () =>
      HttpResponse.json(
        [...stored.values()]
          .filter((item) => !item.confirmed)
          .map((item) =>
            productToCheck({
              product_id: item.id,
              name: item.canonical_name,
              raw_text: PRINTED[item.id] ?? "",
            }),
          ),
      ),
    ),
    http.get("/api/products/:productId", ({ params }) =>
      HttpResponse.json(find(params.productId)),
    ),
    http.post(
      "/api/products/:productId/confirm",
      async ({ params, request }) => {
        const body = (await request.json()) as {
          fields?: Field[] | null;
          expected?: Partial<Record<Field, unknown>> | null;
        };
        backend.confirms.push([Number(params.productId), body]);
        const before = find(params.productId);
        if (changed(before, body.expected)) return refused();
        const fields =
          body.fields ??
          (["canonical_name", "brand", "size", "category", "tags"] as Field[]);
        const sources = { ...before.field_sources };
        for (const field of fields)
          if (shown(before, field) !== null) sources[field] = "person";
        const after = {
          ...before,
          field_sources: sources,
          confirmed: confirmed(sources),
        };
        stored.set(after.id, after);
        return HttpResponse.json(after);
      },
    ),
    http.patch("/api/products/:productId", async ({ params, request }) => {
      const { expected, ...body } = (await request.json()) as {
        canonical_name?: string;
        brand?: string;
        size?: string;
        category?: ProductOut["category"];
        tags?: string[];
        expected?: Partial<Record<Field, unknown>> | null;
      };
      backend.patches.push([Number(params.productId), { ...body, expected }]);
      const before = find(params.productId);
      if (changed(before, expected)) return refused();
      const cleared = new Set<string>();
      const written = new Set<string>();
      for (const [key, value] of Object.entries(body))
        (value === "" || (Array.isArray(value) && value.length === 0)
          ? cleared
          : written
        ).add(key);
      const sources = Object.fromEntries(
        [
          ...Object.entries(before.field_sources),
          ...[...written].map((key) => [key, "person"] as const),
        ].filter(([key]) => !cleared.has(key)),
      );
      const after: ProductOut = {
        ...before,
        canonical_name: body.canonical_name ?? before.canonical_name,
        brand: body.brand === undefined ? before.brand : body.brand || null,
        size_value: body.size === "" ? null : before.size_value,
        size_unit: body.size === "" ? null : before.size_unit,
        category: body.category ?? before.category,
        tags: body.tags ?? before.tags,
        field_sources: sources,
        confirmed: confirmed(sources),
      };
      stored.set(after.id, after);
      return HttpResponse.json(after);
    }),
  );
  return backend;
}

async function fieldRow(label: string): Promise<HTMLElement> {
  return screen.findByRole("listitem", { name: label });
}

function position(): string {
  return (
    /\d+ of \d+ to check/.exec(textOf(screen.getByRole("main")))?.[0] ?? ""
  );
}

describe("Product checking", () => {
  it("shows the product, where it was printed, a marker per field and why", async () => {
    serveQueue(eggs, kiwi);

    renderApp("/products/check");

    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "Kirkland Organic Eggs",
      }),
    ).toBeInTheDocument();
    const main = screen.getByRole("main");
    expect(position()).toBe("1 of 2 to check");
    expect(textOf(main)).toContain("Printed as “KS ORG EGGS 24CT”");
    const rows = await Promise.all(
      ["Name", "Brand", "Size", "Category", "Tags"].map(fieldRow),
    );
    await waitFor(() => {
      expect(textOf(rows[3] ?? main)).toContain("Dairy & eggs");
    });
    expect(rows.map(textOf)).toEqual([
      "NameKirkland Organic EggsAI",
      "BrandKirkland Signaturebarcode",
      "SizeNot set",
      "CategoryDairy & eggsAI",
      "Tagsorganic, free-run",
    ]);
    expect(
      rows.map((row) => within(row).queryAllByRole("button").length),
    ).toEqual([2, 0, 0, 2, 0]);
    expect(textOf(main)).toContain(
      "Why these are marked: The name and category were guessed by Claude from the printed text. Fields from a barcode, the PLU list or you are not marked.",
    );
  });

  it("accepting a guess confirms that field as shown and drops its marker", async () => {
    const backend = serveQueue(eggs);
    const user = userEvent.setup();
    renderApp("/products/check");

    const category = await fieldRow("Category");
    await user.click(
      within(category).getByRole("button", { name: "Accept category" }),
    );

    await waitFor(() => {
      expect(textOf(category)).toMatch(/you$/);
    });
    expect(backend.confirms).toEqual([
      [5, { fields: ["category"], expected: { category: "dairy_eggs" } }],
    ]);
    expect(within(category).queryByRole("button")).toBeNull();
    expect(textOf(await fieldRow("Name"))).toMatch(/AI$/);
  });

  it("a second tap on accept while the first is in flight sends nothing", async () => {
    const backend = serveQueue(eggs);
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.post("/api/products/:productId/confirm", async () => {
        backend.confirms.push([5, null]);
        await gate;
        const after: ProductOut = {
          ...eggs,
          field_sources: { ...eggs.field_sources, category: "person" },
        };
        backend.stored.set(5, after);
        return HttpResponse.json(after);
      }),
    );
    const user = userEvent.setup();
    renderApp("/products/check");

    const accept = within(await fieldRow("Category")).getByRole("button", {
      name: "Accept category",
    });
    await user.click(accept);
    await user.click(accept);
    release();

    await waitFor(() => {
      expect(
        within(screen.getByRole("listitem", { name: "Category" })).queryByRole(
          "button",
        ),
      ).toBeNull();
    });
    expect(backend.confirms).toHaveLength(1);
  });

  it("refuses to confirm a value that changed since it was shown, then shows the new one", async () => {
    const backend = serveQueue(eggs);
    const user = userEvent.setup();
    renderApp("/products/check");

    const category = await fieldRow("Category");
    await waitFor(() => {
      expect(textOf(category)).toContain("Dairy & eggs");
    });
    backend.stored.set(5, { ...eggs, category: "pantry" });
    await user.click(
      within(category).getByRole("button", { name: "Accept category" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This product changed while you were looking. Check the new values and try again.",
    );
    await waitFor(() => {
      expect(textOf(category)).toBe("CategoryPantryAI");
    });
    expect(backend.stored.get(5)?.field_sources.category).toBe("llm");
  });

  it("refuses to clear a rejected value that changed since it was shown, then shows the new one", async () => {
    const backend = serveQueue(kiwi);
    const user = userEvent.setup();
    renderApp("/products/check");

    const brand = await fieldRow("Brand");
    backend.stored.set(6, { ...kiwi, brand: "Sungold" });
    await user.click(
      within(brand).getByRole("button", { name: "Reject brand" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This product changed while you were looking.",
    );
    await waitFor(() => {
      expect(textOf(brand)).toBe("BrandSungoldAI");
    });
    expect(backend.stored.get(6)?.brand).toBe("Sungold");
  });

  it("refuses to save an edited name over one that changed after editing began", async () => {
    const backend = serveQueue(eggs);
    const user = userEvent.setup();
    const { queryClient } = renderApp("/products/check");

    const name = await fieldRow("Name");
    await user.click(within(name).getByRole("button", { name: "Reject name" }));
    backend.stored.set(5, { ...eggs, canonical_name: "Large Eggs" });
    await queryClient.refetchQueries({ queryKey: ["products", "detail", 5] });
    const input = within(name).getByRole("textbox", { name: "New name" });
    await user.clear(input);
    await user.type(input, "Organic Eggs");
    await user.click(within(name).getByRole("button", { name: "Save" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This product changed while you were looking.",
    );
    expect(
      await screen.findByRole("heading", { level: 1, name: "Large Eggs" }),
    ).toBeInTheDocument();
    expect(backend.stored.get(5)?.canonical_name).toBe("Large Eggs");
  });

  it.each(["Brand", "Size", "Tags"])(
    "rejecting a guessed %s clears it",
    async (label) => {
      const backend = serveQueue(kiwi);
      const user = userEvent.setup();
      renderApp("/products/check");

      const row = await fieldRow(label);
      await user.click(
        within(row).getByRole("button", {
          name: `Reject ${label.toLowerCase()}`,
        }),
      );

      await waitFor(() => {
        expect(textOf(row)).toBe(`${label}Not set`);
      });
      expect(within(row).queryByRole("button")).toBeNull();
      const field = label.toLowerCase() as Field;
      expect(backend.patches).toEqual([
        [
          6,
          {
            ...(label === "Tags" ? { tags: [] } : { [field]: "" }),
            expected: { [field]: shown(kiwi, field) },
          },
        ],
      ]);
      expect(backend.confirms).toEqual([]);
    },
  );

  it("rejecting the name opens it for editing and saves what the person types", async () => {
    const backend = serveQueue(eggs);
    const user = userEvent.setup();
    renderApp("/products/check");

    const name = await fieldRow("Name");
    await user.click(within(name).getByRole("button", { name: "Reject name" }));
    const input = within(name).getByRole("textbox", { name: "New name" });
    await user.clear(input);
    expect(within(name).getByRole("button", { name: "Save" })).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "All correct, next" }),
    ).toBeDisabled();
    await user.type(input, "Organic Eggs");
    await user.click(within(name).getByRole("button", { name: "Save" }));

    expect(
      await screen.findByRole("heading", { level: 1, name: "Organic Eggs" }),
    ).toBeInTheDocument();
    expect(backend.patches).toEqual([
      [
        5,
        {
          canonical_name: "Organic Eggs",
          expected: { canonical_name: eggs.canonical_name },
        },
      ],
    ]);
    expect(textOf(name)).toBe("NameOrganic Eggsyou");
  });

  it("rejecting the category lets the person pick another", async () => {
    const backend = serveQueue(eggs);
    const user = userEvent.setup();
    renderApp("/products/check");

    const category = await fieldRow("Category");
    await user.click(
      within(category).getByRole("button", { name: "Reject category" }),
    );
    await user.selectOptions(
      within(category).getByRole("combobox", { name: "New category" }),
      "pantry",
    );
    await user.click(within(category).getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(textOf(category)).toBe("CategoryPantryyou");
    });
    expect(backend.patches).toEqual([
      [5, { category: "pantry", expected: { category: "dairy_eggs" } }],
    ]);
  });

  it("keeps a product on screen after its name is accepted while other guesses wait", async () => {
    serveQueue(eggs, kiwi);
    const user = userEvent.setup();
    renderApp("/products/check");

    const name = await fieldRow("Name");
    await user.click(within(name).getByRole("button", { name: "Accept name" }));
    await waitFor(() => {
      expect(textOf(name)).toMatch(/you$/);
    });

    expect(
      screen.getByRole("heading", { level: 1, name: "Kirkland Organic Eggs" }),
    ).toBeInTheDocument();
    expect(position()).toBe("1 of 2 to check");
    expect(
      within(await fieldRow("Category")).getByRole("button", {
        name: "Accept category",
      }),
    ).toBeInTheDocument();
  });

  it("walks the queue in its first order: all correct confirms as shown, then all checked", async () => {
    const backend = serveQueue(eggs, kiwi);
    const user = userEvent.setup();
    renderApp("/products/check");

    await screen.findByRole("heading", { name: "Kirkland Organic Eggs" });
    await user.click(screen.getByRole("button", { name: "All correct, next" }));

    expect(
      await screen.findByRole("heading", { name: "Gold Kiwi" }),
    ).toBeInTheDocument();
    expect(position()).toBe("2 of 2 to check");
    expect(textOf(screen.getByRole("main"))).toContain(
      "Printed as “GOLD KIWI”",
    );
    expect(backend.confirms).toEqual([
      [
        5,
        {
          expected: {
            canonical_name: "Kirkland Organic Eggs",
            brand: "Kirkland Signature",
            size: null,
            category: "dairy_eggs",
            tags: ["organic", "free-run"],
          },
        },
      ],
    ]);

    await user.click(screen.getByRole("button", { name: "All correct, next" }));

    expect(
      await screen.findByRole("heading", { name: "All checked" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Back to products" }),
    ).toHaveAttribute("href", "/products");
  });

  it("after skipping, the end of the list says how many were skipped and can start again", async () => {
    const backend = serveQueue(eggs, kiwi);
    const user = userEvent.setup();
    renderApp("/products/check");

    await screen.findByRole("heading", { name: "Kirkland Organic Eggs" });
    await user.click(screen.getByRole("button", { name: "Skip" }));
    await screen.findByRole("heading", { name: "Gold Kiwi" });
    await user.click(screen.getByRole("button", { name: "All correct, next" }));

    expect(
      await screen.findByRole("heading", { name: "End of the list" }),
    ).toBeInTheDocument();
    expect(textOf(screen.getByRole("main"))).toContain("1 skipped.");
    expect(screen.queryByRole("heading", { name: "All checked" })).toBeNull();
    expect(backend.confirms.map(([id]) => id)).toEqual([6]);

    await user.click(screen.getByRole("button", { name: "Start again" }));

    expect(
      await screen.findByRole("heading", { name: "Kirkland Organic Eggs" }),
    ).toBeInTheDocument();
    expect(position()).toBe("1 of 1 to check");
  });

  it("drops a product confirmed elsewhere and adds a newly found one at the end", async () => {
    const backend = serveQueue(eggs, kiwi);
    const user = userEvent.setup();
    const { queryClient } = renderApp("/products/check");
    await screen.findByRole("heading", { name: "Kirkland Organic Eggs" });

    backend.stored.set(6, { ...kiwi, confirmed: true });
    backend.stored.set(7, milk);
    await queryClient.refetchQueries({ queryKey: ["products", "to-check"] });

    await waitFor(() => {
      expect(position()).toBe("1 of 2 to check");
    });
    expect(
      screen.getByRole("heading", { level: 1, name: "Kirkland Organic Eggs" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Skip" }));
    expect(
      await screen.findByRole("heading", { name: "Natrel Milk" }),
    ).toBeInTheDocument();
    expect(position()).toBe("2 of 2 to check");
  });

  it("says all checked when nothing waits", async () => {
    renderApp("/products/check");

    expect(
      await screen.findByRole("heading", { name: "All checked" }),
    ).toBeInTheDocument();
  });

  it("fills a queue first shown empty from the cache once the list is read again", async () => {
    serveQueue(eggs);
    const { queryClient, router } = renderApp("/");
    queryClient.setQueryData(["products", "to-check"], []);

    await router.navigate({ to: "/products/check" });

    expect(
      await screen.findByRole("heading", { name: "Kirkland Organic Eggs" }),
    ).toBeInTheDocument();
    expect(position()).toBe("1 of 1 to check");
  });

  it("stays on the product and shows the API's message when confirming fails", async () => {
    serveQueue(eggs);
    const problem: ProblemDetail = {
      type: "/problems/internal",
      title: "Internal Server Error",
      status: 500,
      detail: "An unexpected error occurred.",
      instance: "/api/products/5/confirm",
      errors: null,
    };
    server.use(
      http.post("/api/products/:productId/confirm", () =>
        HttpResponse.json(problem, {
          status: 500,
          headers: { "content-type": "application/problem+json" },
        }),
      ),
    );
    const user = userEvent.setup();
    renderApp("/products/check");

    await screen.findByRole("heading", { name: "Kirkland Organic Eggs" });
    await user.click(screen.getByRole("button", { name: "All correct, next" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "An unexpected error occurred.",
    );
    expect(position()).toBe("1 of 1 to check");
  });
});
