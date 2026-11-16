import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, beforeEach, vi } from "vitest";

import {
  installIntersectionObserver,
  resetIntersectionObserver,
} from "./intersection-observer";
import { installNodeFormData } from "./form-data";
import { installMatchMedia, resetMatchMedia } from "./match-media";
import { server } from "./msw/server";
import { installPointerCapture } from "./pointer-capture";

installPointerCapture();

beforeAll(async () => {
  await installNodeFormData();
  // Any request without a handler is a test bug (and would be real egress in production).
  server.listen({ onUnhandledRequest: "error" });
});

beforeEach(() => {
  installMatchMedia();
  installIntersectionObserver();
});

afterEach(() => {
  cleanup();
  server.resetHandlers();
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
  resetMatchMedia();
  resetIntersectionObserver();
  window.localStorage.clear();
  document.documentElement.className = "";
  document.documentElement.removeAttribute("style");
  document.head.querySelectorAll('meta[name="theme-color"]').forEach((meta) => {
    meta.remove();
  });
});

afterAll(async () => {
  server.close();
  // sonner removes a closing toast with an uncleared 200 ms setTimeout that outlives unmount;
  // let it fire while jsdom's window still exists, or it throws after teardown.
  await new Promise((resolve) => setTimeout(resolve, 250));
});
