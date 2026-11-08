import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import { ApiError, apiClient, unwrap } from "@/api/client";
import { healthyResponse, type ProblemDetail } from "@/test/msw/handlers";
import { server } from "@/test/msw/server";

const notFound: ProblemDetail = {
  type: "/problems/not-found",
  title: "Not Found",
  status: 404,
  detail: "No such thing",
  instance: null,
  errors: null,
};

describe("api client", () => {
  it("S0-AC-40: defaults to same-origin requests (empty base URL)", async () => {
    const urls: string[] = [];
    server.use(
      http.get("/api/health", ({ request }) => {
        urls.push(request.url);
        return HttpResponse.json(healthyResponse);
      }),
    );

    const { data } = await apiClient.GET("/api/health");

    expect(data).toEqual(healthyResponse);
    expect(urls).toEqual([`${window.location.origin}/api/health`]);
  });

  it("S0-AC-40: the base URL comes from build-time VITE_API_BASE_URL", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "http://paper-trail.test:8420");
    vi.resetModules();
    server.use(
      http.get("http://paper-trail.test:8420/api/health", () =>
        HttpResponse.json({ ...healthyResponse, build_sha: "from-env" }),
      ),
    );

    const fresh = await import("@/api/client");
    const { data } = await fresh.apiClient.GET("/api/health");

    expect(data?.build_sha).toBe("from-env");
  });

  it("S0-AC-30: unwrap returns data on success", async () => {
    await expect(unwrap(apiClient.GET("/api/health"))).resolves.toEqual(
      healthyResponse,
    );
  });

  it("S0-AC-30: unwrap throws ApiError carrying the problem details on problem+json", async () => {
    server.use(
      http.get("/api/health", () =>
        HttpResponse.json(notFound, {
          status: 404,
          headers: { "Content-Type": "application/problem+json" },
        }),
      ),
    );

    const error = await unwrap(apiClient.GET("/api/health")).catch(
      (caught: unknown) => caught,
    );

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 404,
      problem: { type: "/problems/not-found" },
    });
  });

  it("S0-AC-30: unwrap throws ApiError without problem on a non-JSON error", async () => {
    server.use(
      http.get(
        "/api/health",
        () => new HttpResponse("Bad gateway", { status: 502 }),
      ),
    );

    const error = await unwrap(apiClient.GET("/api/health")).catch(
      (caught: unknown) => caught,
    );

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 502 });
    expect((error as ApiError).problem).toBeUndefined();
  });
});
