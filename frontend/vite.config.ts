/// <reference types="vitest/config" />
import { fileURLToPath, URL } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    port: 5173,
    strictPort: true,
    // In compose.dev.yaml the API runs in the `web` service (same origin through the proxy).
    proxy: { "/api": { target: "http://web:8000" } },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    coverage: {
      provider: "v8",
      include: ["src/**/*.{ts,tsx}"],
      exclude: [
        "src/**/*.test.{ts,tsx}",
        "src/test/**",
        "src/api/schema.d.ts",
        "src/vite-env.d.ts",
        "src/main.tsx",
      ],
      reporter: ["text", "html"],
      thresholds: { statements: 85, branches: 80, functions: 80, lines: 85 },
    },
    projects: [
      {
        extends: true,
        test: { name: "app", include: ["src/**/*.test.{ts,tsx}"] },
      },
      {
        // Repository-level checks of configs and static files, run in Node without the DOM setup.
        extends: false,
        test: {
          name: "tooling",
          environment: "node",
          include: ["tests/**/*.test.ts"],
        },
      },
    ],
  },
});
