"""Checks executed INSIDE the runtime image by tests/container/run.sh.

Run as: docker run --rm -i --entrypoint python paper-trail:latest - < this_file

Stdlib only: the runtime image has no test tooling (that is one of the things checked).
Covers S0-AC-08 (user, contents), S0-AC-33 (built manifest and icons), S0-AC-38 (no external
URLs in the built bundle). Exits non-zero listing every failed check.
"""

import importlib.util
import json
import os
import re
import shutil
import struct
import sys
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path

STATIC = Path("/app/static")
FORBIDDEN_MODULES = ("pytest", "mypy", "ruff", "hypothesis", "importlinter", "pre_commit")
FORBIDDEN_BINARIES = ("uv", "uvx", "node", "npm", "npx", "pytest", "mypy", "ruff", "gcc", "cc")
# Absolute URLs that are identifiers or documentation links, never fetched at runtime, plus
# the tailwindcss.com link in its MIT licence banner and TanStack Router's loopback fallback
# origin for windowless (SSR) rendering, which is not an external host.
NON_FETCHED_URL = re.compile(
    r"^https?://(www\.w3\.org/|react\.dev/errors|reactjs\.org/|tanstack\.com/|github\.com/"
    r"|tailwindcss\.com\b|localhost\b)"
)
URL = re.compile(r"""https?://[^\s"'`)<>\\]+""")
FETCH_CONTEXT = re.compile(
    r"""(?:src|href|action)\s*=\s*["']?\s*(?:https?:)?//"""
    r"""|url\(\s*["']?(?:https?:)?//"""
    r"""|@import\s+["']?(?:https?:)?//""",
    re.IGNORECASE,
)


class _HeadLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []
        self.metas: list[dict[str, str]] = []
        self.has_root = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k: v or "" for k, v in attrs}
        if tag == "link":
            self.links.append(values)
        elif tag == "meta":
            self.metas.append(values)
        elif tag == "div" and values.get("id") == "root":
            self.has_root = True


def _png_size(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    width, height = struct.unpack(">II", header[16:24])
    return width, height


def _static_file(reference: str) -> Path:
    return STATIC / reference.lstrip("/")


def check_user() -> None:
    """S0-AC-08 runs as non-root UID 1000."""
    assert os.getuid() == 1000, f"uid is {os.getuid()}"
    assert os.getuid() != 0


def check_index() -> None:
    """S0-AC-08 contains /app/static/index.html."""
    assert (STATIC / "index.html").is_file()


def check_no_dev_modules() -> None:
    """S0-AC-08 no dev/test python packages."""
    present = [m for m in FORBIDDEN_MODULES if importlib.util.find_spec(m) is not None]
    assert not present, f"found {present}"


def check_no_toolchains() -> None:
    """S0-AC-08 no uv, node or compilers on PATH."""
    present = [b for b in FORBIDDEN_BINARIES if shutil.which(b) is not None]
    assert not present, f"found {present}"


def check_entry_point() -> None:
    """S0-AC-08 paper-trail entry point is installed (non-editable)."""
    assert shutil.which("paper-trail"), "paper-trail not on PATH"
    spec = importlib.util.find_spec("paper_trail")
    assert spec is not None
    assert spec.origin is not None
    assert "site-packages" in spec.origin, f"paper_trail imported from {spec.origin}"


def check_index_links() -> None:
    """S0-AC-33 built index.html links manifest and apple-touch-icon."""
    parser = _HeadLinks()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    rels = {link.get("rel"): link.get("href", "") for link in parser.links}
    assert rels.get("manifest") == "/manifest.webmanifest", rels
    assert "apple-touch-icon" in rels, rels
    assert _static_file(rels["apple-touch-icon"]).is_file(), rels["apple-touch-icon"]
    viewport = next((m["content"] for m in parser.metas if m.get("name") == "viewport"), "")
    assert "viewport-fit=cover" in viewport, viewport
    assert any(m.get("name") == "theme-color" for m in parser.metas)
    assert parser.has_root, 'index.html has no <div id="root">'


def check_manifest() -> None:
    """S0-AC-33 built manifest fields and icons."""
    manifest = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["name"] == "Paper Trail"
    assert manifest["short_name"]
    assert manifest["start_url"] == "/scan"
    assert manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert manifest["theme_color"]
    assert manifest["background_color"]
    sizes = set()
    for icon in manifest["icons"]:
        path = _static_file(icon["src"])
        assert path.is_file(), f"missing icon {icon['src']}"
        width, height = _png_size(path)
        assert icon["sizes"] == f"{width}x{height}", f"{icon['src']} is {width}x{height}"
        sizes.add(icon["sizes"])
    assert {"192x192", "512x512"} <= sizes, sizes
    assert any("maskable" in icon.get("purpose", "") for icon in manifest["icons"])


def check_no_service_worker() -> None:
    """S0-AC-33 no service worker."""
    bundle = "\n".join(p.read_text(encoding="utf-8") for p in STATIC.rglob("*.js"))
    assert "serviceWorker.register" not in bundle


def check_no_external_urls() -> None:
    """S0-AC-38 bundle loads nothing from external hosts."""
    offenders: list[str] = []
    for path in sorted(STATIC.rglob("*")):
        if path.suffix not in {".html", ".css", ".js", ".webmanifest", ".json", ".svg"}:
            continue
        text = path.read_text(encoding="utf-8")
        if path.suffix in {".html", ".css"} and FETCH_CONTEXT.search(text):
            offenders.append(f"{path.relative_to(STATIC)}: {FETCH_CONTEXT.search(text)}")
        offenders.extend(
            f"{path.relative_to(STATIC)}: {url}"
            for url in URL.findall(text)
            if not NON_FETCHED_URL.match(url)
        )
    assert not offenders, "\n  " + "\n  ".join(sorted(set(offenders)))


CHECKS: list[Callable[[], None]] = [
    check_user,
    check_index,
    check_no_dev_modules,
    check_no_toolchains,
    check_entry_point,
    check_index_links,
    check_manifest,
    check_no_service_worker,
    check_no_external_urls,
]


def main() -> int:
    failures: list[str] = []
    for check in CHECKS:
        name = (check.__doc__ or check.__name__).strip()
        try:
            check()
        except (AssertionError, OSError, KeyError, ValueError) as error:
            failures.append(f"FAIL {name}: {error}")
        else:
            sys.stdout.write(f"ok   {name}\n")
    sys.stderr.write("".join(f"{line}\n" for line in failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
