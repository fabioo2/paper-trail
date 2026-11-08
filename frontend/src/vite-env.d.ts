/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Origin of the API, e.g. for a separately hosted SPA. Empty (default) means same origin. */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
