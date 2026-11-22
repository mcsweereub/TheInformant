"""Headless-browser rendering for JavaScript-built websites.

Some sites send a near-empty page and build the content with JavaScript in the
browser. The plain HTTP crawler can't see that content, so for those pages the
crawler asks a real (invisible) Chromium browser, driven by Playwright, to load
the page and hand back the finished HTML.

Optional: needs the browser downloaded once with `uv run playwright install chromium`.
Without it, rendering raises BrowserUnavailable and the crawler carries on as before.
"""

from __future__ import annotations

from typing import Any, Protocol
from urllib.parse import urlparse

from ..httpsafe import guard_enabled, is_public_host

INSTALL_HINT = "install it with: uv run playwright install chromium"
# Heavy resources that don't affect page text; blocking them makes rendering much faster.
BLOCKED_RESOURCES = {"image", "media", "font"}


class BrowserUnavailable(RuntimeError):
    """Playwright or its Chromium browser isn't installed."""


class Renderer(Protocol):
    def render(self, url: str) -> tuple[str, int | None, str]:
        """Load `url`; return (final url, HTTP status, rendered HTML)."""

    def close(self) -> None: ...


def _route(route: Any) -> None:
    """Skip heavy resources, and refuse any request (the page itself, its scripts' calls,
    redirects) to a non-public address such as this machine or the local network."""
    request = route.request
    if request.resource_type in BLOCKED_RESOURCES:
        route.abort()
        return
    if guard_enabled():
        host = urlparse(request.url).hostname
        if request.url.startswith(("http:", "https:")) and not is_public_host(host):
            route.abort("addressunreachable")
            return
    route.continue_()


class PlaywrightRenderer:
    """Starts Chromium lazily on first use and reuses it for every page."""

    def __init__(self, user_agent: str, *, timeout_ms: int = 20_000, settle_ms: int = 6_000) -> None:
        self.user_agent = user_agent
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None

    def _start(self) -> None:
        try:
            from playwright.sync_api import Error, sync_playwright
        except ImportError as e:
            raise BrowserUnavailable(f"Playwright isn't installed; {INSTALL_HINT}") from e
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(headless=True)
        except Error as e:
            self.close()
            raise BrowserUnavailable(f"Chromium isn't installed; {INSTALL_HINT}") from e
        self._context = self._browser.new_context(
            user_agent=self.user_agent,
            viewport={"width": 1366, "height": 900},
            java_script_enabled=True,
        )
        self._context.route("**/*", _route)

    def render(self, url: str) -> tuple[str, int | None, str]:
        if self._context is None:
            self._start()
        page = self._context.new_page()
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            try:  # let client-side code finish fetching and drawing content
                page.wait_for_load_state("networkidle", timeout=self.settle_ms)
            except Exception:
                pass  # some sites never go idle (analytics, websockets); use what's there
            return page.url, response.status if response else None, page.content()
        finally:
            page.close()

    def close(self) -> None:
        for obj, method in ((self._context, "close"), (self._browser, "close"), (self._pw, "stop")):
            if obj is not None:
                try:
                    getattr(obj, method)()
                except Exception:
                    pass
        self._pw = self._browser = self._context = None
