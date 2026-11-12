"""Polite, small-scale website crawler.

Fetches a company's homepage plus a handful of key pages (chosen by pages.py).
Deliberately conservative:
  * obeys robots.txt (and its Crawl-delay), identifies itself honestly
  * one request at a time with a delay, same host only, HTML only
  * no login/paywall/CAPTCHA bypass: if a site blocks us we note it and move on
  * JavaScript-built pages are retried in a headless browser (renderer.py)
"""

from __future__ import annotations

import os
import time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from .pages import FALLBACK_PATHS, NOT_FOUND_RE, choose_pages, menu_links
from .parse import CrawlResult, Page, looks_js_rendered, normalise_url, parse_page, path_key
from .renderer import BrowserUnavailable, PlaywrightRenderer, Renderer

USER_AGENT = "Mozilla/5.0 (compatible; informant/0.1; personal company research)"
MAX_BYTES = 3_000_000

BrowserMode = str  # "auto" (only when needed), "always", or "never"


class Crawler:
    def __init__(self, *, delay: float = 1.0, timeout: float = 15.0,
                 client: httpx.Client | None = None,
                 browser: BrowserMode | None = None,
                 renderer: Renderer | None = None) -> None:
        self.delay = delay
        mode = (browser or os.environ.get("INFORMANT_BROWSER") or "auto").lower()
        if mode not in ("auto", "always", "never"):
            raise ValueError(f"browser mode must be auto, always or never, not {mode!r}")
        self.browser_mode = mode
        self._renderer = renderer
        self._renderer_broken = False  # set once the browser turns out to be unavailable
        self._js_site = False  # set once a site's homepage needed the browser
        self._http = client or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        )
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_request = 0.0

    def close(self) -> None:
        self._http.close()
        if self._renderer is not None:
            self._renderer.close()

    def __enter__(self) -> Crawler:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- politeness -------------------------------------------------------

    def _wait(self, extra: float = 0.0) -> None:
        gap = max(self.delay, extra) - (time.monotonic() - self._last_request)
        if gap > 0:
            time.sleep(gap)
        self._last_request = time.monotonic()

    def _robots_for(self, url: str) -> RobotFileParser | None:
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self._robots:
            rp = RobotFileParser()
            try:
                resp = self._http.get(f"{origin}/robots.txt")
                if resp.status_code in (401, 403):
                    rp.disallow_all = True
                elif resp.status_code >= 400:
                    rp.allow_all = True
                else:
                    rp.parse(resp.text.splitlines())
            except httpx.HTTPError:
                rp.allow_all = True
            self._robots[origin] = rp
        return self._robots[origin]

    def allowed(self, url: str) -> bool:
        rp = self._robots_for(url)
        return rp is None or rp.can_fetch(USER_AGENT, url)

    def _crawl_delay(self, url: str) -> float:
        rp = self._robots_for(url)
        d = rp.crawl_delay(USER_AGENT) if rp else None
        return min(float(d), 10.0) if d else 0.0

    # --- fetching ---------------------------------------------------------

    def _render(self, url: str, kind: str, notes: list[str]) -> Page | None:
        """Load a page in the headless browser. None if unavailable or it failed."""
        if self.browser_mode == "never" or self._renderer_broken:
            return None
        if self._renderer is None:
            self._renderer = PlaywrightRenderer(USER_AGENT)
        self._wait(self._crawl_delay(url))
        try:
            final_url, status, html = self._renderer.render(url)
        except BrowserUnavailable as e:
            self._renderer_broken = True
            notes.append(f"headless browser unavailable: {e}")
            return None
        except Exception as e:  # timeouts, crashes, navigation errors
            notes.append(f"headless browser failed on {url}: {type(e).__name__}")
            return None
        if status != 200 or not html:
            return Page(url=final_url, kind=kind, status_code=status)
        page = parse_page(final_url, kind, status, html[:MAX_BYTES])
        page.rendered = True
        return page

    def fetch(self, url: str, kind: str, notes: list[str], *, allow_render: bool = True) -> Page | None:
        if not self.allowed(url):
            notes.append(f"robots.txt disallows {url}; skipped")
            return None

        # Known JavaScript site (or --browser always): go straight to the browser.
        if allow_render and (self.browser_mode == "always" or self._js_site):
            page = self._render(url, kind, notes)
            if page is not None and page.status_code == 200 and page.text:
                return page

        page = self._fetch_http(url, kind, notes)

        # Thin, script-heavy page: try again in the browser, keep it if it shows more.
        if (allow_render and self.browser_mode == "auto" and page is not None
                and page.status_code == 200 and looks_js_rendered(page)):
            rendered = self._render(url, kind, notes)
            if rendered is not None and len(rendered.text) > len(page.text):
                rendered.headers = page.headers
                if kind == "home":
                    self._js_site = True
                return rendered
        return page

    def _fetch_http(self, url: str, kind: str, notes: list[str]) -> Page | None:
        self._wait(self._crawl_delay(url))
        try:
            resp = self._http.get(url)
        except httpx.HTTPError as e:
            notes.append(f"failed to fetch {url}: {type(e).__name__}")
            return None
        if resp.status_code in (401, 403, 429, 503):
            notes.append(f"{url} returned {resp.status_code} (blocked or rate limited); skipped")
            return Page(url=str(resp.url), kind=kind, status_code=resp.status_code)
        ctype = resp.headers.get("content-type", "")
        if resp.status_code != 200 or "html" not in ctype:
            return Page(url=str(resp.url), kind=kind, status_code=resp.status_code)
        html = resp.text[:MAX_BYTES]
        page = parse_page(str(resp.url), kind, resp.status_code, html)
        page.headers = dict(resp.headers)
        return page

    def crawl(self, start_url: str, max_pages: int = 8) -> CrawlResult:
        start_url = normalise_url(start_url)
        result = CrawlResult(start_url=start_url)
        home = self.fetch(start_url, "home", result.notes)
        if home is None or home.status_code != 200:
            if home is not None and not result.notes:
                result.notes.append(f"homepage returned {home.status_code}")
            if home:
                result.pages.append(home)
            return result

        result.final_url = home.url
        result.headers = home.headers
        result.pages.append(home)
        if home.rendered:
            result.notes.append(
                "JavaScript-built site: pages were loaded with a headless browser")
        elif looks_js_rendered(home):
            why = ("browser rendering is off (--browser never)" if self.browser_mode == "never"
                   else "the headless browser couldn't help")
            result.notes.append(
                "homepage has very little text and lots of scripts: probably "
                f"JavaScript-rendered, so content may be missing ({why})")

        chosen = choose_pages(home, max_extra=max_pages - 1)
        for url, kind in chosen:
            page = self.fetch(url, kind, result.notes)
            if page:
                result.pages.append(page)

        # Kinds the homepage didn't link to: try the usual addresses directly.
        found_kinds = {kind for _, kind in chosen}
        for kind, paths in FALLBACK_PATHS.items():
            if kind in found_kinds or len(result.pages) >= max_pages:
                continue
            for path in paths:
                url = urljoin(home.url, path)
                if url.rstrip("/") in {p.url.rstrip("/") for p in result.pages}:
                    continue
                page = self.fetch(url, kind, [])
                if (page and page.status_code == 200 and page.text
                        and not NOT_FOUND_RE.search(page.title or "")):
                    result.pages.append(page)
                    break

        # Still room: take the homepage's other menu links (e.g. /eos/ for a product
        # named on the menu, which no keyword would match).
        room = max_pages - len(result.pages)
        if room > 0:
            taken = {path_key(p.url) for p in result.pages} | {path_key(u) for u, _ in chosen}
            for url in menu_links(home, taken, room):
                page = self.fetch(url, "other", result.notes)
                if page:
                    result.pages.append(page)
        return result

    def probe(self, url: str) -> Page | None:
        """Fetch a single page quietly (used for website discovery)."""
        notes: list[str] = []
        return self.fetch(normalise_url(url), "home", notes, allow_render=False)
