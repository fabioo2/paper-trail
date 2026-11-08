import { http, HttpResponse } from "msw";

import type { components } from "@/api/schema";
import { receiptPage } from "@/test/fixtures/receipts";

export type HealthOut = components["schemas"]["HealthOut"];
export type ProblemDetail = components["schemas"]["ProblemDetail"];

export const healthyResponse: HealthOut = {
  status: "ok",
  version: "0.1.0",
  build_sha: null,
};

/**
 * Default handlers: a healthy, same-origin backend with no receipts yet.
 * Tests override them with `server.use(...)`.
 */
export const handlers = [
  http.get("/api/health", () => HttpResponse.json(healthyResponse)),
  http.get("/api/receipts", () => HttpResponse.json(receiptPage([]))),
];
