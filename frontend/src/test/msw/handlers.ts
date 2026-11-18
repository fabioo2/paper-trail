import { http, HttpResponse } from "msw";

import type { components } from "@/api/schema";
import { categories } from "@/test/fixtures/products";
import { receiptPage } from "@/test/fixtures/receipts";
import { monthSummary, searchResults } from "@/test/fixtures/summary";

export type HealthOut = components["schemas"]["HealthOut"];
export type ProblemDetail = components["schemas"]["ProblemDetail"];

export const healthyResponse: HealthOut = {
  status: "ok",
  version: "0.1.0",
  build_sha: null,
};

/**
 * Default handlers: a healthy, same-origin backend with no receipts, products or reviews yet.
 * Tests override them with `server.use(...)`.
 */
export const handlers = [
  http.get("/api/health", () => HttpResponse.json(healthyResponse)),
  http.get("/api/receipts", () => HttpResponse.json(receiptPage([]))),
  http.get("/api/products", () => HttpResponse.json([])),
  http.get("/api/products/to-check", () => HttpResponse.json([])),
  http.get("/api/products/:productId/purchases", () => HttpResponse.json([])),
  http.get("/api/reviews", () => HttpResponse.json([])),
  http.get("/api/categories", () => HttpResponse.json(categories)),
  http.get("/api/summary", () => HttpResponse.json(monthSummary())),
  http.get("/api/search", () => HttpResponse.json(searchResults())),
];
