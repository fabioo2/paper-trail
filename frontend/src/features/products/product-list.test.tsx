import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { formatDate } from "@/features/receipts/dates";
import { formatMoney } from "@/features/receipts/money";
import { productSummary } from "@/test/fixtures/products";
import { cad } from "@/test/fixtures/receipts";
import type { ProblemDetail } from "@/test/msw/handlers";
import { server } from "@/test/msw/server";
import { renderApp } from "@/test/render";
import { textOf } from "@/test/text";

const SKELETON = '[data-slot="skeleton"]';

const eggs = productSummary();
const milk = productSummary({
  id: 6,
  canonical_name: "Natrel 2% Milk",
  brand: "Natrel",
  size_value: "4",
  size_unit: "l",
  category: "pantry",
  tags: ["lactose-free"],
  field_sources: { canonical_name: "person" },
  confirmed: true,
  last_purchase: {
    purchased_on: null,
    store_name: "Farm Boy",
    paid: cad(649),
  },
});

const serverError: ProblemDetail = {
  type: "/problems/internal",
  title: "Internal Server Error",
  status: 500,
  detail: "An unexpected error occurred.",
  instance: "/api/products",
  errors: null,
};

function serveProducts(): void {
  server.use(http.get("/api/products", () => HttpResponse.json([eggs, milk])));
}

async function productLinks(): Promise<HTMLElement[]> {
  const main = await screen.findByRole("main");
  return within(main).findAllByRole("link", { name: /eggs|milk/i });
}

describe("Products tab", () => {
  it("shows skeleton rows while the products load", async () => {
    server.use(
      http.get("/api/products", async () => {
        await delay("infinite");
        return HttpResponse.json([]);
      }),
    );

    renderApp("/products");

    const main = await screen.findByRole("main");
    await waitFor(() => {
      expect(main.querySelectorAll(SKELETON).length).toBeGreaterThan(0);
    });
  });

  it("shows the empty state when nothing was bought yet", async () => {
    renderApp("/products");

    const main = await screen.findByRole("main");
    expect(
      await within(main).findByRole("heading", { name: "No products yet" }),
    ).toBeInTheDocument();
    expect(textOf(main)).toContain(
      "Every product on the receipts you scan shows up here with where you last bought it.",
    );
  });

  it("lists each product with brand, size, category, last purchase and a suggested badge", async () => {
    serveProducts();

    renderApp("/products");

    const [eggsRow, milkRow] = await productLinks();
    if (!eggsRow || !milkRow) throw new Error("expected two product rows");
    expect(eggsRow).toHaveAttribute("href", "/products/5");
    for (const part of [
      "Kirkland Organic Eggs",
      "Kirkland Signature · 24 each",
      `${formatDate("2026-11-01")} · COSTCO WHOLESALE #1234`,
      formatMoney(cad(899)),
      "Dairy & eggs",
    ])
      expect(textOf(eggsRow)).toContain(part);
    for (const part of [
      "Natrel · 4 l",
      "Date unknown · Farm Boy",
      formatMoney(cad(649)),
      "Pantry",
    ])
      expect(textOf(milkRow)).toContain(part);
    expect(within(eggsRow).getByText("Suggested")).toBeInTheDocument();
    expect(within(milkRow).queryByText("Suggested")).toBeNull();
  });

  it.each([
    ["a name", "eggs", "Kirkland Organic Eggs"],
    ["a brand", "NATREL", "Natrel 2% Milk"],
    ["a tag", "lactose", "Natrel 2% Milk"],
  ])("filters by %s", async (_, query, name) => {
    serveProducts();
    const user = userEvent.setup();
    renderApp("/products");
    await productLinks();

    await user.type(
      screen.getByRole("searchbox", { name: "Search products" }),
      query,
    );

    const rows = within(screen.getByRole("main")).getAllByRole("link");
    expect(rows.map(textOf)).toEqual([expect.stringContaining(name)]);
  });

  it("says so when nothing matches the search", async () => {
    serveProducts();
    const user = userEvent.setup();
    renderApp("/products");
    await productLinks();

    await user.type(screen.getByRole("searchbox"), "zucchini");

    expect(
      screen.getByRole("heading", { name: "No matching products" }),
    ).toBeInTheDocument();
  });

  it("Needs a look shows only the unconfirmed products, with their count", async () => {
    serveProducts();
    const user = userEvent.setup();
    renderApp("/products");
    await productLinks();

    const toggle = screen.getByRole("button", { name: /needs a look/i });
    expect(textOf(toggle)).toBe("Needs a look1");
    await user.click(toggle);

    expect(toggle).toHaveAttribute("aria-pressed", "true");
    expect(await productLinks()).toHaveLength(1);
    expect(screen.queryByRole("link", { name: /milk/i })).toBeNull();

    await user.click(toggle);
    expect(await productLinks()).toHaveLength(2);
  });

  it("shows an error toast with a retry action when loading fails", async () => {
    let fail = true;
    server.use(
      http.get("/api/products", () =>
        fail
          ? HttpResponse.json(serverError, { status: 500 })
          : HttpResponse.json([eggs, milk]),
      ),
    );
    const user = userEvent.setup();

    renderApp("/products");

    expect(
      await screen.findByText("Products are unavailable"),
    ).toBeInTheDocument();
    const retry = await screen.findByRole("button", { name: /retry/i });
    fail = false;
    await user.click(retry);

    expect(await productLinks()).toHaveLength(2);
  });
});
