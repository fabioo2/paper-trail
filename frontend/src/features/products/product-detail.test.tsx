import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { formatDate, formatMonth } from "@/features/receipts/dates";
import { formatMoney } from "@/features/receipts/money";
import {
  product,
  productPurchase,
  type ProductPurchaseOut,
} from "@/test/fixtures/products";
import { cad } from "@/test/fixtures/receipts";
import type { ProblemDetail } from "@/test/msw/handlers";
import { server } from "@/test/msw/server";
import { renderApp } from "@/test/render";
import { textOf } from "@/test/text";

const notFound: ProblemDetail = {
  type: "/problems/not-found",
  title: "Not Found",
  status: 404,
  detail: null,
  instance: "/api/products/424242",
  errors: null,
};

const purchases: ProductPurchaseOut[] = [
  productPurchase({
    receipt_id: 12,
    purchased_on: "2026-11-03",
    store_name: "LOBLAWS #1012",
    unit_price: cad(1699),
    discount: cad(-400),
    paid: cad(1299),
  }),
  productPurchase({
    receipt_id: 11,
    purchased_on: "2026-11-02",
    quantity: "2",
    unit_price: cad(609),
    paid: cad(1218),
  }),
  productPurchase({ receipt_id: 10, purchased_on: null }),
];

function serveProduct(
  bought: ProductPurchaseOut[],
  overrides: Partial<ReturnType<typeof product>> = {},
): void {
  server.use(
    http.get("/api/products/:productId", () =>
      HttpResponse.json(product(overrides)),
    ),
    http.get("/api/products/:productId/purchases", () =>
      HttpResponse.json(bought),
    ),
  );
}

async function pricesSection(): Promise<HTMLElement> {
  const heading = await screen.findByRole("heading", {
    level: 2,
    name: "Prices",
  });
  const section = heading.closest("section");
  if (!section) throw new Error("expected the prices section");
  return section;
}

async function purchasesSection(): Promise<HTMLElement> {
  const heading = await screen.findByRole("heading", {
    level: 2,
    name: /^Purchases/,
  });
  const section = heading.closest("section");
  if (!section) throw new Error("expected the purchases section");
  return section;
}

describe("Product detail", () => {
  it("shows the product once with its purchases newest first, each linking to its receipt", async () => {
    serveProduct(purchases);

    renderApp("/products/5");

    const main = await screen.findByRole("main");
    expect(
      await within(main).findByRole("heading", {
        level: 1,
        name: "Kirkland Organic Eggs",
      }),
    ).toBeInTheDocument();
    expect(
      await within(main).findByText(
        "Kirkland Signature · 24 each · Dairy & eggs",
      ),
    ).toBeInTheDocument();
    const section = await purchasesSection();
    const rows = within(section).getAllByRole("link");
    expect(textOf(section)).toContain("Purchases (3)");
    expect(rows.map((row) => row.getAttribute("href"))).toEqual([
      "/receipts/12",
      "/receipts/11",
      "/receipts/10",
    ]);
    expect(rows.map(textOf)).toEqual([
      `${formatDate("2026-11-03")} · Loblawssave ${formatMoney(cad(400))}${formatMoney(cad(1299))}`,
      `${formatDate("2026-11-02")} · Costco× 2${formatMoney(cad(1218))}`,
      `Date unknown · Costco${formatMoney(cad(899))}`,
    ]);
    expect(screen.queryByLabelText(/^name/i)).toBeNull();
  });

  it("says so when the product was never bought, with no prices", async () => {
    serveProduct([]);

    renderApp("/products/5");

    const section = await purchasesSection();
    expect(textOf(section)).toBe(
      "Purchases (0)No purchases yet. Scan a receipt with this product to see what you paid.",
    );
    expect(screen.queryByRole("heading", { name: "Prices" })).toBeNull();
  });

  it.each([
    ["a single purchase", [productPurchase()]],
    [
      "two purchases, one undated",
      [productPurchase(), productPurchase({ purchased_on: null })],
    ],
  ])(
    "shows the store table but no chart or verdict for %s",
    async (_, bought) => {
      serveProduct(bought);

      renderApp("/products/5");

      const section = await pricesSection();
      expect(within(section).getAllByRole("row")).toHaveLength(2);
      expect(within(section).queryByRole("img")).toBeNull();
      expect(textOf(section)).not.toContain("cheaper");
    },
  );

  it("charts each store's price per unit, compares the stores and names the cheapest", async () => {
    serveProduct([
      productPurchase({
        purchased_on: "2026-11-10",
        store_name: "METRO #77",
        quantity: "2",
        unit: "kg",
        paid: cad(1000),
      }),
      productPurchase({
        purchased_on: "2026-10-01",
        quantity: "1",
        unit: "kg",
        paid: cad(470),
      }),
      productPurchase({
        purchased_on: "2026-09-01",
        quantity: "1",
        unit: "kg",
        paid: cad(450),
      }),
      productPurchase({ purchased_on: "2026-08-01", unit: "each" }),
    ]);

    renderApp("/products/5");

    const section = await pricesSection();
    const chart = within(section).getByRole("img");
    expect(chart).toHaveAccessibleName(
      `Price per kg, ${formatMonth("2026-09-01")} to ${formatMonth("2026-11-10")}. Costco: 2 purchases from ${formatMoney(cad(450))} to ${formatMoney(cad(470))}, last ${formatMoney(cad(470))}. Metro: 1 purchase, ${formatMoney(cad(500))}`,
    );
    expect(
      [...chart.querySelectorAll("[data-store]")].map((line) => [
        line.getAttribute("data-store"),
        line.querySelectorAll("[data-point]").length,
        line.querySelector("polyline")?.getAttribute("stroke-dasharray") ??
          null,
        line.querySelector("[data-point]")?.tagName,
      ]),
    ).toEqual([
      ["Costco", 2, null, "circle"],
      ["Metro", 1, "6 3", "rect"],
    ]);
    expect(within(section).getAllByRole("listitem").map(textOf)).toEqual([
      "Costco",
      "Metro",
    ]);
    expect(within(section).getAllByRole("row").map(textOf)).toEqual([
      "Per kgLowAvgLast",
      `Costco (cheapest)${formatMoney(cad(450))}${formatMoney(cad(460))}${formatMoney(cad(470))}`,
      `Metro${formatMoney(cad(500))}${formatMoney(cad(500))}${formatMoney(cad(500))}`,
    ]);
    expect(textOf(section)).toContain(
      "Costco is 8% cheaper per kg than Metro across your last 3 purchases",
    );
  });

  it("prices a sized product per litre, and with one purchase per store says what happened last time", async () => {
    serveProduct(
      [
        productPurchase({
          purchased_on: "2026-11-10",
          store_name: "METRO #77",
          quantity: "2",
          paid: cad(1300),
        }),
        productPurchase({ purchased_on: "2026-11-01", paid: cad(609) }),
      ],
      { size_value: "4", size_unit: "l" },
    );

    renderApp("/products/5");

    const section = await pricesSection();
    expect(within(section).getByRole("img")).toHaveAccessibleName(
      expect.stringMatching(/^Price per L, /),
    );
    expect(within(section).getAllByRole("row").map(textOf)).toEqual([
      "Per LLowAvgLast",
      `Costco (cheapest)${formatMoney(cad(152))}${formatMoney(cad(152))}${formatMoney(cad(152))}`,
      `Metro${formatMoney(cad(163))}${formatMoney(cad(163))}${formatMoney(cad(163))}`,
    ]);
    expect(textOf(section)).toContain(
      "Last time, Costco was 6% cheaper per L than Metro",
    );
  });

  it("applies the chosen range to the chart, the table and the verdict", async () => {
    serveProduct([
      productPurchase({
        purchased_on: "2026-11-10",
        store_name: "METRO #77",
        paid: cad(1000),
      }),
      productPurchase({ purchased_on: "2026-10-01", paid: cad(900) }),
      productPurchase({
        purchased_on: "2026-08-01",
        store_name: "METRO #77",
        paid: cad(1200),
      }),
      productPurchase({ purchased_on: "2025-12-01", paid: cad(700) }),
    ]);
    const user = userEvent.setup();
    renderApp("/products/5");
    const section = await pricesSection();
    const range = within(section).getByRole("group", { name: "Chart range" });

    expect(
      within(range).getByRole("button", { name: "6M", pressed: true }),
    ).toBeInTheDocument();
    expect(within(section).getByRole("img")).toHaveAccessibleName(
      expect.stringContaining(`Costco: 1 purchase, ${formatMoney(cad(900))}`),
    );
    expect(within(section).getAllByRole("row")).toHaveLength(3);
    expect(textOf(section)).toContain(
      "Costco is 18% cheaper per item than Metro in the last 6 months",
    );

    await user.click(within(range).getByRole("button", { name: "1M" }));
    expect(within(section).queryByRole("img")).toBeNull();
    expect(textOf(section)).toContain(
      "Fewer than two purchases in this range.",
    );
    expect(within(section).getAllByRole("row").map(textOf)).toEqual([
      "Per itemLowAvgLast",
      `Metro${formatMoney(cad(1000))}${formatMoney(cad(1000))}${formatMoney(cad(1000))}`,
    ]);
    expect(textOf(section)).not.toContain("cheaper");

    await user.click(within(range).getByRole("button", { name: "All" }));
    expect(within(section).getByRole("img")).toHaveAccessibleName(
      expect.stringContaining(
        `Costco: 2 purchases from ${formatMoney(cad(700))} to ${formatMoney(cad(900))}`,
      ),
    );
    expect(textOf(section)).toContain(
      "Costco is 27% cheaper per item than Metro across your last 4 purchases",
    );
  });

  it("shows weighed prices per kg or lb", async () => {
    serveProduct([
      productPurchase({
        receipt_id: 21,
        quantity: "0.5",
        unit: "kg",
        unit_price: cad(441),
        paid: cad(220),
      }),
      productPurchase({
        receipt_id: 20,
        purchased_on: "2026-10-30",
        quantity: "2",
        unit: "lb",
        unit_price: cad(200),
        paid: cad(400),
      }),
    ]);

    renderApp("/products/5");

    const section = await purchasesSection();
    expect(within(section).getAllByRole("link").map(textOf)).toEqual([
      `${formatDate("2026-11-01")} · Costco0.5 kg · ${formatMoney(cad(440))}/kg${formatMoney(cad(220))}`,
      `${formatDate("2026-10-30")} · Costco2 lb · ${formatMoney(cad(200))}/lb${formatMoney(cad(400))}`,
    ]);
  });

  it("keeps Cancel disabled while a save is in flight", async () => {
    serveProduct(purchases);
    let release: () => void = () => undefined;
    server.use(
      http.patch("/api/products/:productId", async () => {
        await new Promise<void>((resolve) => {
          release = resolve;
        });
        return HttpResponse.json(product());
      }),
    );
    const user = userEvent.setup();
    renderApp("/products/5");
    await purchasesSection();
    await user.click(screen.getByRole("button", { name: "Edit" }));

    await user.clear(screen.getByLabelText(/^size/i));
    await user.type(screen.getByLabelText(/^size/i), "12 each");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    });
    release();
    expect(await screen.findByText("Product saved")).toBeInTheDocument();
  });

  it("opens the editor with Edit and closes it with Cancel", async () => {
    serveProduct(purchases);
    const user = userEvent.setup();
    renderApp("/products/5");
    await purchasesSection();

    await user.click(screen.getByRole("button", { name: "Edit" }));

    expect(screen.getByLabelText(/^name/i)).toHaveValue(
      "Kirkland Organic Eggs",
    );
    expect(
      screen.getByRole("button", { name: "Looks right" }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByLabelText(/^name/i)).toBeNull();
  });

  it("shows not found for an unknown product", async () => {
    server.use(
      http.get("/api/products/:productId", async () => {
        await delay(0);
        return HttpResponse.json(notFound, {
          status: 404,
          headers: { "content-type": "application/problem+json" },
        });
      }),
    );

    renderApp("/products/424242");

    expect(
      await screen.findByRole("heading", { name: "Product not found" }),
    ).toBeInTheDocument();
  });
});
