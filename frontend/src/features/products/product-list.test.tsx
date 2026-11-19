import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { formatMoney } from "@/features/receipts/money";
import { pricePoint, productSummary } from "@/test/fixtures/products";
import { cad } from "@/test/fixtures/receipts";
import type { ProblemDetail } from "@/test/msw/handlers";
import { server } from "@/test/msw/server";
import { renderApp } from "@/test/render";
import { textOf } from "@/test/text";

const SKELETON = '[data-slot="skeleton"]';
const COSTCO = "COSTCO WHOLESALE #1234";
const METRO = "METRO #77";

const eggs = productSummary({
  times_bought: 3,
  last_purchase: {
    purchased_on: "2026-11-01",
    store_name: COSTCO,
    paid: cad(1199),
  },
  recent: [
    pricePoint({ paid: cad(1199) }),
    pricePoint({ purchased_on: "2026-10-01", paid: cad(1599) }),
    pricePoint({
      purchased_on: "2026-09-01",
      store_name: METRO,
      paid: cad(1599),
    }),
  ],
});
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
  times_bought: 1,
  last_purchase: {
    purchased_on: "2026-11-02",
    store_name: COSTCO,
    paid: cad(609),
  },
  recent: [pricePoint({ purchased_on: "2026-11-02", paid: cad(609) })],
});
const kiwi = productSummary({
  id: 7,
  canonical_name: "Gold Kiwi",
  brand: null,
  size_value: null,
  size_unit: null,
  category: "produce",
  tags: [],
  times_bought: 2,
  last_purchase: {
    purchased_on: "2026-11-03",
    store_name: METRO,
    paid: cad(168),
  },
  recent: [
    pricePoint({ store_name: METRO, paid: cad(168) }),
    pricePoint({ store_name: METRO, paid: cad(155) }),
  ],
});
const pepper = productSummary({
  id: 8,
  canonical_name: "Orange Pepper",
  brand: null,
  size_value: null,
  size_unit: null,
  category: null,
  tags: [],
  field_sources: {},
  times_bought: 2,
  last_purchase: {
    purchased_on: "2026-11-03",
    store_name: METRO,
    paid: cad(226),
  },
  recent: [
    pricePoint({
      store_name: METRO,
      quantity: "0.452",
      unit: "kg",
      paid: cad(226),
    }),
    pricePoint({
      store_name: METRO,
      quantity: "0.5",
      unit: "kg",
      paid: cad(250),
    }),
  ],
});
const undated = productSummary({
  id: 9,
  canonical_name: "Undated Bread",
  category: "pantry",
  times_bought: 2,
  last_purchase: { purchased_on: null, store_name: METRO, paid: cad(399) },
  recent: [],
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
  server.use(
    http.get("/api/products", () =>
      HttpResponse.json([kiwi, eggs, milk, pepper, undated]),
    ),
  );
}

async function productRows(): Promise<HTMLElement[]> {
  const main = await screen.findByRole("main");
  const list = await within(main).findByRole("list");
  return within(list).getAllByRole("link");
}

function names(rows: HTMLElement[]): string[] {
  return rows.map((row) => row.querySelector("p")?.textContent ?? "");
}

function chips(): HTMLElement {
  return screen.getByRole("group", { name: "Filter products" });
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

  it("shows each product's store, count, unit price, last price, trend and change against its average, with the category dot as its only marker", async () => {
    serveProducts();

    renderApp("/products");

    const rows = await productRows();
    expect(rows[1]).toHaveAttribute("href", "/products/5");
    expect(rows.map(textOf)).toEqual([
      `Gold KiwiMetro · 2×${formatMoney(cad(168))}↑up 8%`,
      `Kirkland Organic EggsCostco · 3×${formatMoney(cad(1199))}↓down 25%`,
      `Natrel 2% MilkCostco · 1× · ${formatMoney(cad(152))}/L${formatMoney(cad(609))}first time`,
      `Orange PepperMetro · 2× · ${formatMoney(cad(500))}/kg${formatMoney(cad(226))}steady`,
      `Undated BreadMetro · 2×${formatMoney(cad(399))}`,
    ]);
    expect(
      rows.map((row) => row.querySelector("[data-sparkline]") !== null),
    ).toEqual([true, true, false, true, false]);
    expect(
      screen.queryAllByRole("img", { name: "Has details Claude guessed" }),
    ).toEqual([]);
    expect(
      rows.map((row) =>
        row.querySelector("[data-category]")?.getAttribute("style"),
      ),
    ).toEqual([
      "background-color: var(--cat-produce);",
      "background-color: var(--cat-dairy-eggs);",
      "background-color: var(--cat-pantry);",
      undefined,
      "background-color: var(--cat-pantry);",
    ]);
  });

  it("offers a chip for every category and store present, and the count to check", async () => {
    serveProducts();

    renderApp("/products");
    await productRows();

    expect(within(chips()).getAllByRole("button").map(textOf)).toEqual([
      "All",
      "Produce",
      "Dairy & eggs",
      "Pantry",
      "Costco",
      "Metro",
      "To check4",
    ]);
    expect(
      within(chips()).getByRole("button", { name: "All", pressed: true }),
    ).toBeInTheDocument();
  });

  it.each([
    [
      "a category",
      "Pantry",
      { category: "pantry" },
      ["Natrel 2% Milk", "Undated Bread"],
    ],
    [
      "a store, counting earlier purchases there",
      "Metro",
      { store: "Metro" },
      ["Gold Kiwi", "Kirkland Organic Eggs", "Orange Pepper", "Undated Bread"],
    ],
    [
      "the products to check",
      /^To check/,
      { needsALook: true },
      ["Gold Kiwi", "Kirkland Organic Eggs", "Orange Pepper", "Undated Bread"],
    ],
  ])("filters by %s, kept in the URL", async (_, chip, search, expected) => {
    serveProducts();
    const user = userEvent.setup();
    const { router } = renderApp("/products");
    await productRows();

    await user.click(within(chips()).getByRole("button", { name: chip }));

    expect(names(await productRows())).toEqual(expected);
    expect(router.state.location.search).toEqual(search);
    expect(
      within(chips()).getByRole("button", { name: chip, pressed: true }),
    ).toBeInTheDocument();

    await user.click(within(chips()).getByRole("button", { name: "All" }));
    expect(await productRows()).toHaveLength(5);
    expect(router.state.location.search).toEqual({});
  });

  it.each([
    ["/products?category=produce", ["Gold Kiwi"]],
    ["/products?store=Costco", ["Kirkland Organic Eggs", "Natrel 2% Milk"]],
    [
      "/products?needsALook=true",
      ["Gold Kiwi", "Kirkland Organic Eggs", "Orange Pepper", "Undated Bread"],
    ],
  ])("opens %s on its filter", async (path, expected) => {
    serveProducts();

    renderApp(path);

    expect(names(await productRows())).toEqual(expected);
  });

  it.each([
    ["a name", "eggs", "Kirkland Organic Eggs"],
    ["a brand", "NATREL", "Natrel 2% Milk"],
    ["a tag", "lactose", "Natrel 2% Milk"],
  ])("searches by %s", async (_, query, name) => {
    serveProducts();
    const user = userEvent.setup();
    renderApp("/products");
    await productRows();

    await user.type(
      screen.getByRole("searchbox", { name: "Search products" }),
      query,
    );

    await waitFor(async () => {
      expect(names(await productRows())).toEqual([name]);
    });
  });

  it("combines the header search with a chip in the URL, and says so when nothing matches", async () => {
    serveProducts();
    const user = userEvent.setup();
    const { router } = renderApp("/products");
    await productRows();
    await user.click(within(chips()).getByRole("button", { name: "Costco" }));

    await user.type(
      screen.getByRole("searchbox", { name: "Search products" }),
      "e",
    );
    await waitFor(() => {
      expect(router.state.location.search).toEqual({ store: "Costco", q: "e" });
    });
    expect(names(await productRows())).toEqual([
      "Kirkland Organic Eggs",
      "Natrel 2% Milk",
    ]);

    await user.type(
      screen.getByRole("searchbox", { name: "Search products" }),
      "ggs",
    );
    await user.click(within(chips()).getByRole("button", { name: "Pantry" }));
    expect(
      await screen.findByRole("heading", { name: "No matching products" }),
    ).toBeInTheDocument();
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

    expect(await productRows()).toHaveLength(2);
  });
});
