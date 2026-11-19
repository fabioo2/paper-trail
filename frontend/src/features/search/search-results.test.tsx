import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { SEARCH_DEBOUNCE_MS } from "@/components/layout/header-search";
import { Highlight } from "@/features/search/search-results";
import { productSummary } from "@/test/fixtures/products";
import { cad, receiptSummary } from "@/test/fixtures/receipts";
import { searchResults } from "@/test/fixtures/summary";
import { server } from "@/test/msw/server";
import { renderApp } from "@/test/render";
import { textOf } from "@/test/text";

const kiwi = searchResults({
  products: [
    productSummary({
      id: 9,
      canonical_name: "Gold Kiwi",
      brand: null,
      category: "produce",
      times_bought: 3,
      last_purchase: {
        purchased_on: "2026-11-05",
        store_name: "COSTCO WHOLESALE #1234",
        paid: cad(1199),
      },
    }),
  ],
  receipts: [
    {
      ...receiptSummary({
        id: 4,
        purchased_on: "2026-11-05",
        total: cad(20319),
      }),
      lines: ["Gold Kiwi", "Kiwi Sorbet"],
    },
  ],
});

function serveSearch(): string[] {
  const queries: string[] = [];
  server.use(
    http.get("/api/search", ({ request }) => {
      queries.push(new URL(request.url).searchParams.get("q") ?? "");
      return HttpResponse.json(kiwi);
    }),
  );
  return queries;
}

async function searchField(): Promise<HTMLElement> {
  return screen.findByRole("searchbox", {
    name: "Search receipts and products",
  });
}

describe("Header search", () => {
  it("opens grouped results with the matches marked, with the exact copy", async () => {
    const user = userEvent.setup();
    const queries = serveSearch();
    const { router } = renderApp("/");

    await user.type(await searchField(), "  kiwi ");

    const products = await screen.findByRole("region", { name: "Products" });
    expect(router.state.location.pathname).toBe("/search");
    expect(router.state.location.search).toEqual({ q: "kiwi" });
    expect(queries).toEqual(["kiwi"]);
    expect(textOf(products)).toBe(
      "Products1Gold KiwiProduce · bought 3×Last bought at Costco, Nov 5$11.99",
    );
    expect(
      within(products).getByRole("link", { name: /gold kiwi/i }),
    ).toHaveAttribute("href", "/products/9");
    const receipts = screen.getByRole("region", { name: "Receipts" });
    expect(textOf(receipts)).toBe(
      "Receipts1CostcoNov 5 · Gold Kiwi, Kiwi Sorbet$203.19",
    );
    expect(within(receipts).getByRole("link")).toHaveAttribute(
      "href",
      "/receipts/4",
    );
    expect(
      [...document.querySelectorAll("main mark")].map(
        (mark) => mark.textContent,
      ),
    ).toEqual(["Kiwi", "Kiwi", "Kiwi"]);
  });

  it("asks for two letters before searching", async () => {
    const queries = serveSearch();

    renderApp("/search?q=k");

    expect(
      await screen.findByRole("heading", { name: "Search your purchases" }),
    ).toBeInTheDocument();
    expect(queries).toEqual([]);
  });

  it("says when nothing matches", async () => {
    renderApp("/search?q=zzz");

    expect(
      await screen.findByRole("heading", { name: "Nothing found" }),
    ).toBeInTheDocument();
  });

  it("goes back Home when the field is cleared, and leaving the results clears it", async () => {
    const user = userEvent.setup();
    serveSearch();
    const { router } = renderApp("/search?q=kiwi");
    await screen.findByRole("region", { name: "Products" });
    expect(await searchField()).toHaveValue("kiwi");

    await user.clear(await searchField());

    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/");
    });

    await user.type(await searchField(), "kiwi");
    await screen.findByRole("region", { name: "Products" });
    await user.click(
      within(screen.getByRole("navigation", { name: "Primary" })).getByRole(
        "link",
        { name: "Receipts" },
      ),
    );

    await screen.findByRole("heading", { name: "No receipts yet" });
    expect(await searchField()).toHaveValue("");
  });

  it("keeps the query and results when Back returns from a result", async () => {
    const user = userEvent.setup();
    serveSearch();
    const { router } = renderApp("/");
    await user.type(await searchField(), "kiwi");
    const receipts = await screen.findByRole("region", { name: "Receipts" });
    await user.click(within(receipts).getByRole("link"));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/receipts/4");
    });
    expect(await searchField()).toHaveValue("");

    router.history.back();

    expect(
      await screen.findByRole("region", { name: "Receipts" }),
    ).toBeInTheDocument();
    expect(await searchField()).toHaveValue("kiwi");
    await new Promise((resolve) => setTimeout(resolve, SEARCH_DEBOUNCE_MS * 2));
    expect(router.state.location.pathname).toBe("/search");
  });

  it("filters the list on Products instead of opening results", async () => {
    const user = userEvent.setup();
    const queries = serveSearch();
    const { router } = renderApp("/products");
    await screen.findByRole("heading", { name: "No products yet" });
    expect(
      screen.queryByRole("searchbox", { name: "Search receipts and products" }),
    ).toBeNull();

    await user.type(
      screen.getByRole("searchbox", { name: "Search products" }),
      "kiwi",
    );

    await waitFor(() => {
      expect(router.state.location.search).toEqual({ q: "kiwi" });
    });
    expect(router.state.location.pathname).toBe("/products");
    expect(queries).toEqual([]);
  });

  it("drops what was typed on Products when another page opens before it is sent", async () => {
    const user = userEvent.setup();
    const queries = serveSearch();
    const { router } = renderApp("/products");
    await screen.findByRole("heading", { name: "No products yet" });

    await user.type(
      screen.getByRole("searchbox", { name: "Search products" }),
      "kiwi",
    );
    await router.navigate({ to: "/receipts" });
    await new Promise((resolve) => setTimeout(resolve, SEARCH_DEBOUNCE_MS * 2));

    expect(router.state.location.pathname).toBe("/receipts");
    expect(await searchField()).toHaveValue("");
    expect(queries).toEqual([]);
  });

  it("shows a product's new name when the search is repeated after an edit", async () => {
    const user = userEvent.setup();
    let name = "Gold Kiwi";
    const stored = () => productSummary({ id: 9, canonical_name: name });
    server.use(
      http.get("/api/search", () =>
        HttpResponse.json(searchResults({ products: [stored()] })),
      ),
      http.get("/api/products", () => HttpResponse.json([stored()])),
      http.get("/api/products/:productId", () => HttpResponse.json(stored())),
      http.patch("/api/products/:productId", () => {
        name = "Golden Kiwi";
        return HttpResponse.json(stored());
      }),
    );
    const { router } = renderApp("/search?q=kiwi");
    await user.click(await screen.findByRole("link", { name: /gold kiwi/i }));
    const main = await screen.findByRole("main");
    await within(main).findByRole("heading", { level: 1, name: /gold kiwi/i });
    await user.click(within(main).getByRole("button", { name: "Edit" }));
    await user.clear(screen.getByLabelText(/^name/i));
    await user.type(screen.getByLabelText(/^name/i), "Golden Kiwi");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText("Product saved");

    await router.navigate({ to: "/search", search: { q: "kiwi" } });

    expect(
      await screen.findByRole("link", { name: /golden kiwi/i }),
    ).toBeInTheDocument();
  });
});

describe("Highlight", () => {
  it.each([
    ["Gold Kiwi", "ki", ["Ki"]],
    ["Skiing kiwis", "KI", ["ki"]],
    ["Kiwi & kale", "kale kiwi", ["Kiwi", "kale"]],
    ["Crème fraîche", "fraî", ["fraî"]],
    ["(a+b) total", "(a+", ["a"]],
    ["Bread", "  ", []],
  ])("marks words of %j starting like %j", (text, query, marked) => {
    const { container } = render(<Highlight text={text} query={query} />);

    expect(
      [...container.querySelectorAll("mark")].map((mark) => mark.textContent),
    ).toEqual(marked);
    expect(container.textContent).toBe(text);
  });
});
