import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

/** Absolute path of the frontend/ directory, independent of the working directory. */
export const FRONTEND_ROOT = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
);

export function frontendPath(...parts: string[]): string {
  return resolve(FRONTEND_ROOT, ...parts);
}
