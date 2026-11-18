# 20. Host and Origin checks instead of a login

Date: 2026-11-18

## Status

Accepted

## Context

The web app has no login, by the owner's choice: it is reached only on the home LAN and over WireGuard. Its API changes data (uploads, review answers, product edits, retries) and spends money (each upload is a Claude read). Being off the internet does not stop a page on another site, opened in a browser on the LAN, from driving the API:

- A cross-site form or `fetch` with a "simple" body (a multipart upload) is sent without a CORS preflight; the browser only hides the answer.
- DNS rebinding makes the attacker's host name resolve to the server's LAN address, so the browser treats the API as same-origin and lets the page read the answers too.

The MCP server already refuses unknown `Host` headers for the same reason (ADR-0013).

## Decision

- The web app serves only the `Host` names `localhost`, `127.0.0.1`, `[::1]` and `web` (the compose service name, used by the smoke test and the Vite proxy), plus those listed in `PAPER_TRAIL_WEB__ALLOWED_HOSTS` (a comma list of bare host names or IPs, e.g. `192.168.0.187,fabio-server`; an IPv6 address with or without brackets). Any port is accepted. Another `Host` is a 400 problem. This defeats DNS rebinding: the rebound request carries the attacker's host name.
- A `POST`, `PATCH`, `PUT` or `DELETE` whose `Origin` header is not the request's own origin (its scheme, `Host` and port) is a 403 problem; `Origin: null` included. A request without `Origin` (curl, the CLI, scripts) is served: browsers always send `Origin` on cross-site writes. Safe methods are not checked, and no CORS headers are sent.
- The comparison uses the request's own scheme and `Host`; `X-Forwarded-*` headers are not trusted, as no reverse proxy is deployed.
- Both checks are one ASGI middleware (`HostOriginMiddleware`), not Starlette's `TrustedHostMiddleware`, so that refusals are problem+json like every other error and are logged with the request id.

## Consequences

- Good: another site's page can neither post to the app nor read from it, without adding a login.
- Good: curl and scripts keep working without extra headers.
- Bad: reaching the app by a new name (another host name, a new LAN address) needs that name in `PAPER_TRAIL_WEB__ALLOWED_HOSTS`; until then the app answers 400.
- Bad: anyone who can reach the app directly (on the LAN or the VPN) can still use it; this is the boundary the owner accepted.
