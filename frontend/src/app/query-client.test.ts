import { describe, expect, it } from "vitest";

import { ApiError } from "@/api/client";
import { createQueryClient } from "@/app/query-client";

function retryPolicy(): (failureCount: number, error: Error) => boolean {
  const retry = createQueryClient().getDefaultOptions().queries?.retry;
  if (typeof retry !== "function") throw new Error("expected a retry function");
  return retry;
}

describe("createQueryClient", () => {
  it("never retries a client error (4xx will not fix itself)", () => {
    expect(retryPolicy()(0, new ApiError(404))).toBe(false);
  });

  it("retries server and network errors twice", () => {
    const retry = retryPolicy();

    expect(retry(0, new ApiError(503))).toBe(true);
    expect(retry(1, new TypeError("Failed to fetch"))).toBe(true);
    expect(retry(2, new ApiError(503))).toBe(false);
  });
});
