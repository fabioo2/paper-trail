# 14. React SPA served by FastAPI from the same origin

Date: 2026-11-06

## Status

Accepted

## Context

The app is reached from a phone over LAN or WireGuard on plain HTTP. A second web server or origin would add CORS configuration and another service.

## Decision

The Node build stage produces `frontend/dist`, which is copied into the runtime image and served by FastAPI. `/api/*` is the JSON API; any other GET returns a static file if it exists, otherwise `index.html` (client-side routing). Hashed assets get `Cache-Control: public, max-age=31536000, immutable`; `index.html` and the manifest get `no-cache`. No CORS middleware is installed. In development, the Vite dev server proxies `/api` to the `web` service.

## Consequences

- One port, one container, no CORS.
- PWA install works (manifest + icons); no service worker, since that would need HTTPS and offline uploads are not useful.
