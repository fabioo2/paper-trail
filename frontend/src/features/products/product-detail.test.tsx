import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { formatDate } from "@/features/receipts/dates";
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

function serveProduct(bought: ProductPurchaseOut[]): void {
  server.use(
    http.get("/api/products/:productId", () => HttpResponse.json(product())),
    http.get("/api/products/:productId/purchases", () =>
      HttpResponse.json(bought),
    ),
  );
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
    expect(textOf(section)).toContain(
      `Lowest ${formatMoney(cad(609))} · average ${formatMoney(cad(936))} per unit`,
    );
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

  it("says so when the product was never bought, with no summary", async () => {
    serveProduct([]);

    renderApp("/products/5");

    const section = await purchasesSection();
    expect(textOf(section)).toBe(
      "Purchases (0)No purchases yet. Scan a receipt with this product to see what you paid.",
    );
  });

  it("shows no summary for a single purchase", async () => {
    serveProduct([productPurchase()]);

    renderApp("/products/5");

    const section = await purchasesSection();
    expect(within(section).getAllByRole("link")).toHaveLength(1);
    expect(textOf(section)).not.toContain("Lowest");
  });

  it.each([
    [
      "keeps full precision until the final rounding",
      [
        productPurchase({ quantity: "10", paid: cad(1001) }),
        productPurchase({ quantity: "5", paid: cad(503) }),
      ],
      `Lowest ${formatMoney(cad(100))} · average ${formatMoney(cad(100))} per unit`,
    ],
    [
      "counts only purchases in the newest purchase's currency",
      [
        productPurchase({ paid: cad(899) }),
        productPurchase({ paid: { amount_cents: 100, currency: "USD" } }),
        productPurchase({ paid: cad(1299) }),
      ],
      `Lowest ${formatMoney(cad(899))} · average ${formatMoney(cad(1099))} per unit`,
    ],
  ])("the summary %s", async (_, bought, summary) => {
    serveProduct(bought);

    renderApp("/products/5");

    expect(textOf(await purchasesSection())).toContain(summary);
  });

  it("shows weighed prices per kg or lb, and the summary in the newest purchase's unit", async () => {
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
    expect(textOf(section)).toContain(
      `Lowest ${formatMoney(cad(440))}/kg · average ${formatMoney(cad(440))}/kg`,
    );
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
