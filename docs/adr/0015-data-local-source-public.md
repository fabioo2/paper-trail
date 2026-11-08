# 15. Data stays on private hardware; source is public on GitHub

Date: 2026-11-06

## Status

Accepted

## Context

Paper Trail stores photos of the owner's receipts and a record of their purchases. The owner wants that data to stay on their own machines. The owner also wants the project on GitHub so that anyone can set it up and so it can be shown as a portfolio piece.

## Decision

- Source code is published on GitHub. CI (GitHub Actions) and Dependabot run there. CI has no access to the owner's network and the repo holds no secrets.
- Receipt images, the database, `.env` files and real evaluation receipts are git-ignored and never leave the owner's hardware. Only synthetic sample receipts are committed.
- CI calls the same Make targets as the local gate (`make check`, `make ci`), so results match.
- At runtime the only outbound request is from the app host to the configured Ollama endpoint. No telemetry, error-reporting SDKs, CDNs, web fonts or analytics; all frontend assets are bundled.
- Ports bind to loopback by default and to a LAN/WireGuard address when configured, never to `0.0.0.0` on the host. Remote access is by WireGuard only.

## Consequences

- Good: anyone can clone and run it; upgrades are proposed and tested automatically.
- Good: personal data never reaches a third party.
- Bad: a mistake could commit personal data; mitigated by `.gitignore`, gitleaks in `make check` and CI, and a pre-commit hook.

## Amendment (2026-11-06): the MCP tool transport

The MCP gateway (agent core slice B) is an HTTP client too, but it only reaches the app host's
own MCP server: a loopback port inside the same command (`process --transport mcp`), or the
`mcp` compose service, published on `127.0.0.1` only. No request leaves the app host except the
one to Ollama.
