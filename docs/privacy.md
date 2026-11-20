# Data and privacy

Only source code is published. Receipt images, the database, `.env` and evaluation receipts stay
on your machines and are git-ignored. The app has no telemetry and loads nothing from CDNs.

**There is no login.** The app is meant for your home network and WireGuard only; never publish
its port to the internet. Anyone who can reach it can use it. What stops another website, opened
in a browser on your network, from using it through that browser is two checks
([ADR-0020](adr/0020-host-and-origin-checks.md)): only the host names in
`PAPER_TRAIL_WEB__ALLOWED_HOSTS` (and localhost) are served, and a write from a browser must come
from the app's own page. The MCP server is published on loopback only.

**The whole photo of every receipt you process is sent to Anthropic** to be read by Claude, and
a new product's printed text, store, department and code to have it guessed. The only other
outbound traffic is a product's barcode, alone, to Open Food Facts and its sister databases
(and to UPCitemdb when you turn it on). Whatever the photo shows leaves your server: the card number
as the terminal printed it (usually masked to the last four digits), the store, a loyalty
number, sometimes your name. Nothing is redacted from the pixels. Card numbers in Claude's
answer are masked before the answer reaches the graph's checkpoints, the traces or the
database; the trace files record each photo's SHA-256, never the photo, are as private as the
database and are never logged. (LangGraph brings the LangSmith client as a dependency; it sends
nothing unless `LANGSMITH_TRACING` is set, which Paper Trail never does.)
