"""Polite, small-scale website crawler.

Fetches a company's homepage plus a handful of key pages (chosen by pages.py).
Deliberately conservative:
  * obeys robots.txt (and its Crawl-delay), identifies itself honestly
  * one request at a time with a delay, same host only, HTML only
  * no login/paywall/CAPTCHA bypass: if a site blocks us we note it and move on
  * JavaScript-built pages are retried in a headless browser (renderer.py)
"""

from __future__ import annotations

import html
import os
import re
import time
from datetime import timedelta
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from ...cache import PageCache
from ...schema import utcnow
from .pages import (
    FALLBACK_PATHS,
    NOT_FOUND_RE,
    PAGE_KINDS,
    choose_pages,
    menu_links,
    sitemap_candidates,
)
from .parse import CrawlResult, Page, looks_js_rendered, normalise_url, parse_page, path_key
from .renderer import BrowserUnavailable, PlaywrightRenderer, Renderer

USER_AGENT = "Mozilla/5.0 (compatible; informant/0.1; personal company research)"
MAX_BYTES = 3_000_000
ROBOTS_MAX_AGE = timedelta(days=1)
MAX_SITEMAP_FILES = 3
MAX_SITEMAP_URLS = 3_000
_OFF = {"0", "off", "false", "no"}
_LOC_RE = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</loc>", re.I | re.S)
# Child sitemaps that list individual posts, products or media rather than key pages.
_BULK_SITEMAP_RE = re.compile(r"post|blog|product|image|video|news|tag|categor|author", re.I)

def decode_body(resp: httpx.Response) -> str:
    """Page text, decoded as UTF-8 whenever the bytes are valid UTF-8.

    Some servers label UTF-8 pages with an old encoding (or none), which turns
    dashes, quotes and pound signs into sequences like 'â€“'. Valid UTF-8 is
    almost never anything else, so trust the bytes over the label.
    """
    try:
        return resp.content.decode("utf-8")
    except UnicodeDecodeError:
        return resp.text


BrowserMode = str  # "auto" (only when needed), "always", or "never"


class Crawler:
    def __init__(self, *, delay: float = 1.0, timeout: float = 15.0,
                 client: httpx.Client | None = None,
                 browser: BrowserMode | None = None,
                 renderer: Renderer | None = None,
                 cache: PageCache | None = None,
                 sitemap: bool | None = None) -> None:
        self.delay = delay
        self.cache = cache  # None = no page cache
        if sitemap is None:
            sitemap = os.environ.get("INFORMANT_SITEMAP", "on").strip().lower() not in _OFF
        self.use_sitemap = sitemap
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
            robots_url = f"{origin}/robots.txt"
            # Reused for a day at most, so a site's new rules are picked up quickly.
            hit = self.cache.get(robots_url, max_age=ROBOTS_MAX_AGE) if self.cache else None
            status, text = (hit.status_code, hit.html) if hit else (None, "")
            if hit is None:
                try:
                    resp = self._http.get(robots_url)
                    status, text = resp.status_code, resp.text[:MAX_BYTES]
                    if self.cache is not None:
                        self.cache.put(robots_url, final_url=robots_url, status_code=status,
                                       html=text if status < 400 else "", headers={},
                                       rendered=False, fetched_at=utcnow())
                except httpx.HTTPError:
                    status = None
            if status in (401, 403):
                rp.disallow_all = True
            elif status is None or status >= 400:
                rp.allow_all = True
            else:
                rp.parse(text.splitlines())
            self._robots[origin] = rp
        return self._robots[origin]

    def allowed(self, url: str) -> bool:
        rp = self._robots_for(url)
        return rp is None or rp.can_fetch(USER_AGENT, url)

    def _crawl_delay(self, url: str) -> float:
        rp = self._robots_for(url)
        d = rp.crawl_delay(USER_AGENT) if rp else None
        return min(float(d), 10.0) if d else 0.0

    # --- sitemap ----------------------------------------------------------

    def _fetch_text(self, url: str) -> str | None:
        """Fetch a small text resource (a sitemap), politely and through the cache.
        A missing file is cached too, as empty text, so it isn't asked for again."""
        if self.cache is not None:
            hit = self.cache.get(url)
            if hit is not None:
                return hit.html or None
        if not self.allowed(url):
            return None
        self._wait(self._crawl_delay(url))
        try:
            resp = self._http.get(url)
        except httpx.HTTPError:
            return None
        text = resp.text[:MAX_BYTES] if resp.status_code == 200 else ""
        if self.cache is not None:
            self.cache.put(url, final_url=str(resp.url), status_code=resp.status_code, html=text,
                           headers={}, rendered=False, fetched_at=utcnow())
        return text or None

    def sitemap_urls(self, home_url: str) -> list[str]:
        """Page addresses listed in the site's sitemap (declared in robots.txt, or at
        /sitemap.xml). Reads at most a few files; an index file's page sitemaps
        are preferred over its post, product and image ones."""
        p = urlparse(home_url)
        origin = f"{p.scheme}://{p.netloc}"
        rp = self._robots_for(home_url)
        declared = (rp.site_maps() or []) if rp is not None else []
        queue = list(declared)[:2] or [f"{origin}/sitemap.xml"]
        urls: list[str] = []
        read = 0
        while queue and read < MAX_SITEMAP_FILES and len(urls) < MAX_SITEMAP_URLS:
            sitemap = queue.pop(0)
            read += 1
            if sitemap.lower().endswith(".gz"):
                continue
            text = self._fetch_text(sitemap)
            if not text:
                continue
            locs = [html.unescape(m.strip()) for m in _LOC_RE.findall(text)]
            if "<sitemapindex" in text.lower():
                locs.sort(key=lambda u: (not re.search(r"page", u, re.I),
                                         bool(_BULK_SITEMAP_RE.search(u))))
                queue.extend(locs)
            else:
                urls.extend(locs)
        return urls[:MAX_SITEMAP_URLS]

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
        page.fetched_at = utcnow()
        return page

    # --- cache ------------------------------------------------------------

    def _from_cache(self, url: str, kind: str) -> Page | None:
        if self.cache is None:
            return None
        hit = self.cache.get(url)
        if hit is None:
            return None
        page = parse_page(hit.final_url, kind, hit.status_code, hit.html)
        page.headers = hit.headers
        page.rendered = hit.rendered
        page.fetched_at = hit.fetched_at
        page.from_cache = True
        return page

    def _store(self, url: str, page: Page | None) -> Page | None:
        if (self.cache is not None and page is not None and not page.from_cache
                and page.status_code == 200 and page.html):
            self.cache.put(url, final_url=page.url, status_code=200, html=page.html,
                           headers=page.headers, rendered=page.rendered,
                           fetched_at=page.fetched_at or utcnow())
        return page

    def _cache_hit_is_enough(self, page: Page, allow_render: bool) -> bool:
        """A cached copy fetched without the browser may be a JavaScript shell (discovery
        probes never render), so only reuse it when rendering couldn't add anything."""
        if page.rendered or not allow_render or self.browser_mode == "never":
            return True
        if self.browser_mode == "always":
            return False
        return not looks_js_rendered(page)

    def fetch(self, url: str, kind: str, notes: list[str], *, allow_render: bool = True) -> Page | None:
        cached = self._from_cache(url, kind)
        if cached is not None and self._cache_hit_is_enough(cached, allow_render):
            if cached.rendered and kind == "home":
                self._js_site = True
            return cached  # no request made, so nothing to ask robots.txt

        if not self.allowed(url):
            notes.append(f"robots.txt disallows {url}; skipped")
            return None

        # Known JavaScript site (or --browser always): go straight to the browser.
        if allow_render and (self.browser_mode == "always" or self._js_site):
            page = self._render(url, kind, notes)
            if page is not None and page.status_code == 200 and page.text:
                return self._store(url, page)

        page = cached or self._fetch_http(url, kind, notes)

        # Thin, script-heavy page: try again in the browser, keep it if it shows more.
        if (allow_render and self.browser_mode == "auto" and page is not None
                and page.status_code == 200 and looks_js_rendered(page)):
            rendered = self._render(url, kind, notes)
            if rendered is not None and len(rendered.text) > len(page.text):
                rendered.headers = page.headers
                if kind == "home":
                    self._js_site = True
                return self._store(url, rendered)
        return self._store(url, page)

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
        body = decode_body(resp)[:MAX_BYTES]
        page = parse_page(str(resp.url), kind, resp.status_code, body)
        page.headers = dict(resp.headers)
        page.fetched_at = utcnow()
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

        # Kinds the homepage didn't link to: look for them in the site's sitemap.
        missing = [k for k in PAGE_KINDS if k not in {kind for _, kind in chosen}]
        room = max_pages - len(result.pages)
        if self.use_sitemap and missing and room > 0:
            candidates = sitemap_candidates(self.sitemap_urls(home.url), home.url)
            taken = {path_key(p.url) for p in result.pages} | {path_key(u) for u, _ in chosen}
            from_sitemap = choose_pages(home, max_extra=room, links=candidates,
                                        kinds=missing, taken=taken)
            for url, kind in from_sitemap:
                page = self.fetch(url, kind, result.notes)
                if page:
                    result.pages.append(page)
            if from_sitemap:
                result.notes.append(
                    f"{len(from_sitemap)} page(s) found through the site's sitemap: "
                    + ", ".join(kind for _, kind in from_sitemap))
                chosen = chosen + from_sitemap

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

        cached = [p for p in result.pages if p.from_cache]
        if cached:
            oldest = min(p.fetched_at for p in cached if p.fetched_at)
            result.notes.append(
                f"{len(cached)} of {len(result.pages)} pages came from the local cache "
                f"(fetched {oldest:%Y-%m-%d}); use --refresh to re-fetch")
        return result

    def probe(self, url: str) -> Page | None:
        """Fetch a single page quietly (used for website discovery)."""
        notes: list[str] = []
        return self.fetch(normalise_url(url), "home", notes, allow_render=False)
