import createClient from "openapi-fetch";

import type { components, paths } from "@/api/schema";

export type ProblemDetail = components["schemas"]["ProblemDetail"];

const PROBLEM_MEDIA_TYPE = "application/problem+json";

/**
 * Where the API lives. Empty (the default) means the origin that served the app; it is
 * resolved to an absolute URL because fetch outside a browser page rejects relative URLs.
 */
const configuredBaseUrl = import.meta.env.VITE_API_BASE_URL ?? "";
export const apiBaseUrl =
  configuredBaseUrl === "" ? window.location.origin : configuredBaseUrl;

/** Typed client generated from the backend's OpenAPI document (ADR-0007). */
export const apiClient = createClient<paths>({
  baseUrl: apiBaseUrl,
  // Resolve fetch per request so wrappers installed after start-up (e.g. test doubles) apply.
  fetch: (request) => globalThis.fetch(request),
});

/** A non-2xx API response, with its RFC 9457 problem details when the server sent them. */
export class ApiError extends Error {
  readonly status: number;
  readonly problem?: ProblemDetail;

  constructor(status: number, problem?: ProblemDetail) {
    super(
      problem?.detail ??
        problem?.title ??
        `Request failed with status ${status}`,
    );
    this.name = "ApiError";
    this.status = status;
    if (problem) this.problem = problem;
  }
}

function isProblemDetail(value: unknown): value is ProblemDetail {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.type === "string" &&
    typeof candidate.title === "string" &&
    typeof candidate.status === "number"
  );
}

interface ClientResult<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

/** Resolve to the response body of a 2xx, or throw an ApiError for anything else. */
export async function unwrap<T>(request: Promise<ClientResult<T>>): Promise<T> {
  const { data, error, response } = await request;
  if (!response.ok) {
    const isProblem = response.headers
      .get("content-type")
      ?.startsWith(PROBLEM_MEDIA_TYPE);
    throw new ApiError(
      response.status,
      isProblem && isProblemDetail(error) ? error : undefined,
    );
  }
  return data as T;
}
