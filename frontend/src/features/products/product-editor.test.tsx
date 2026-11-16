import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import {
  product,
  productSummary,
  type ProductOut,
} from "@/test/fixtures/products";
import type { ProblemDetail } from "@/test/msw/handlers";
import { server } from "@/test/msw/server";
import { renderApp } from "@/test/render";

const sizeProblem: ProblemDetail = {
  type: "/problems/validation",
  title: "Unprocessable Entity",
  status: 422,
  detail:
    "size 'a dozen': give a positive number and one of g, kg, ml, l, each",
  instance: "/api/products/5",
  errors: null,
};

interface Requests {
  list: number;
  patches: unknown[];
  confirms: number;
}

/** Serve the product, its list row, and record every list read, PATCH body and confirm. */
function serveProduct(stored: ProductOut = product()): Requests {
  const requests: Requests = { list: 0, patches: [], confirms: 0 };
  server.use(
    http.get("/api/products", () => {
      requests.list += 1;
      return HttpResponse.json([productSummary(stored)]);
    }),
    http.get("/api/products/:productId", () => HttpResponse.json(stored)),
    http.patch("/api/products/:productId", async ({ request }) => {
      requests.patches.push(await request.json());
      return HttpResponse.json(stored);
    }),
    http.post("/api/products/:productId/confirm", () => {
      requests.confirms += 1;
      return HttpResponse.json({ ...stored, confirmed: true });
    }),
  );
  return requests;
}

async function form(): Promise<HTMLElement> {
  const main = await screen.findByRole("main");
  await within(main).findByRole("heading", { level: 1, name: /eggs/i });
  return main;
}

describe("Product editor", () => {
  it("prefills the fields and marks the guessed ones suggested", async () => {
    serveProduct();

    renderApp("/products/5");
    await form();

    expect(screen.getByLabelText(/^name/i)).toHaveValue(
      "Kirkland Organic Eggs",
    );
    expect(screen.getByLabelText(/^brand/i)).toHaveValue("Kirkland Signature");
    expect(screen.getByLabelText(/^size/i)).toHaveValue("24 each");
    expect(screen.getByLabelText(/^tags/i)).toHaveValue("organic, free-run");
    await waitFor(() => {
      expect(screen.getByLabelText(/^category/i)).toHaveValue("dairy_eggs");
    });
    const suggested = ["Name", "Brand", "Size", "Category", "Tags"].filter(
      (label) =>
        within(screen.getByText(label, { selector: "label" })).queryByText(
          "Suggested",
        ) !== null,
    );
    expect(suggested).toEqual(["Name", "Category"]);
  });

  it("saves only the changed fields, then returns to the refreshed list", async () => {
    const requests = serveProduct();
    const user = userEvent.setup();
    const { router } = renderApp("/products");
    await user.click(await screen.findByRole("link", { name: /eggs/i }));
    await form();

    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeDisabled();
    await user.clear(screen.getByLabelText(/^size/i));
    await user.type(screen.getByLabelText(/^size/i), "12 each");
    await user.clear(screen.getByLabelText(/^tags/i));
    await user.type(screen.getByLabelText(/^tags/i), "eggs, , organic");
    await user.click(save);

    expect(await screen.findByText("Product saved")).toBeInTheDocument();
    expect(requests.patches).toEqual([
      { size: "12 each", tags: ["eggs", "organic"] },
    ]);
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/products");
    });
    await waitFor(() => {
      expect(requests.list).toBe(2);
    });
  });

  it("sends only what the person changed when the product refreshes underneath", async () => {
    serveProduct();
    const user = userEvent.setup();
    const { queryClient } = renderApp("/products/5");
    await form();

    await user.clear(screen.getByLabelText(/^size/i));
    await user.type(screen.getByLabelText(/^size/i), "12 each");
    const refreshed = serveProduct(product({ brand: "Costco" }));
    await queryClient.refetchQueries({ queryKey: ["products"] });
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(refreshed.patches).toEqual([{ size: "12 each" }]);
    });
  });

  it("Looks right confirms the guessed values", async () => {
    const requests = serveProduct();
    const user = userEvent.setup();
    renderApp("/products/5");
    await form();

    await user.click(screen.getByRole("button", { name: "Looks right" }));

    expect(await screen.findByText("Product confirmed")).toBeInTheDocument();
    expect(requests.confirms).toBe(1);
    expect(requests.patches).toEqual([]);
  });

  it("offers no Looks right for a confirmed product", async () => {
    serveProduct(product({ confirmed: true, field_sources: {} }));

    renderApp("/products/5");
    await form();

    expect(screen.queryByRole("button", { name: "Looks right" })).toBeNull();
  });

  it("disables the buttons while saving and shows the API's message on failure", async () => {
    serveProduct();
    let release: () => void = () => undefined;
    server.use(
      http.patch("/api/products/:productId", async () => {
        await new Promise<void>((resolve) => {
          release = resolve;
        });
        return HttpResponse.json(sizeProblem, {
          status: 422,
          headers: { "content-type": "application/problem+json" },
        });
      }),
    );
    const user = userEvent.setup();
    renderApp("/products/5");
    await form();

    await user.clear(screen.getByLabelText(/^size/i));
    await user.type(screen.getByLabelText(/^size/i), "a dozen");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    });
    release();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      sizeProblem.detail ?? "",
    );
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });

  it("shows not found for an unknown product", async () => {
    server.use(
      http.get("/api/products/:productId", async () => {
        await delay(0);
        return HttpResponse.json(
          { ...sizeProblem, status: 404, title: "Not Found", detail: null },
          {
            status: 404,
            headers: { "content-type": "application/problem+json" },
          },
        );
      }),
    );

    renderApp("/products/424242");

    expect(
      await screen.findByRole("heading", { name: "Product not found" }),
    ).toBeInTheDocument();
  });
});
