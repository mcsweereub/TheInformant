"""Polite, small-scale website crawler.

Fetches a company's homepage plus a handful of key pages (about, products,
careers, contact, news). Deliberately conservative:
  * obeys robots.txt (and its Crawl-delay), identifies itself honestly
  * one request at a time with a delay, same host only, HTML only
  * no login/paywall/CAPTCHA bypass: if a site blocks us we note it and move on
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urldefrag, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from .renderer import BrowserUnavailable, PlaywrightRenderer, Renderer

USER_AGENT = "Mozilla/5.0 (compatible; informant/0.1; personal company research)"
MAX_BYTES = 3_000_000

# Page kinds we look for, in priority order (earlier kinds win when max_pages is
# small), with keywords matched against URL path segments and short link labels.
PAGE_KINDS: dict[str, tuple[str, ...]] = {
    "about": ("about", "who-we-are", "our-story", "our-company", "company"),
    "products": ("product", "service", "solution", "what-we-do", "platform", "shop", "menu"),
    "investors": ("investor", "annual-report", "results"),
    "customers": ("case-stud", "customer", "client", "portfolio", "success-stor"),
    "careers": ("career", "jobs", "join-us", "work-with-us", "vacancies", "recruit"),
    "contact": ("contact", "find-us", "locations", "offices", "stores"),
    "news": ("news", "press", "media", "blog", "insights"),
}

# Legal and regulatory pages: long boilerplate that crowds out useful text.
# Judged by URL and link label, which is far more reliable than judging the prose.
LEGAL_RE = re.compile(
    r"terms|conditions|privacy|cookie|legal|polic(?:y|ies)|disclaimer|gdpr|accessib|"
    r"slavery|complaint|sitemap|regulatory|important-information|service-information|"
    r"service-quality|information-about|whistleblow|tax-strategy|gender-pay",
    re.I,
)
# Pages tried directly when the homepage doesn't link to one of that kind.
FALLBACK_PATHS: dict[str, tuple[str, ...]] = {"about": ("/about", "/about-us")}
_NOT_FOUND_RE = re.compile(r"\b(404|page not found|can.t find (that|this) page)\b", re.I)
# Links that never make useful research pages when filling spare slots.
_UTILITY_RE = re.compile(
    r"log-?in|sign-?in|sign-?up|register|account|basket|cart|checkout|search|wp-admin|"
    r"wp-login|feed|/tag/|/category/|/author/",
    re.I,
)
_LANGUAGE_PREFIX_RE = re.compile(r"^/[a-z]{2}(?:-[a-z]{2})?(?:/|$)", re.I)  # /fr/, /en-gb/

_SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|svg|webp|zip|docx?|xlsx?|pptx?|mp4|mp3)$", re.I)


@dataclass
class Page:
    url: str
    kind: str
    status_code: int | None
    html: str = ""
    title: str | None = None
    text: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)  # (absolute url, link text)
    headers: dict[str, str] = field(default_factory=dict)
    rendered: bool = False  # loaded through the headless browser


@dataclass
class CrawlResult:
    start_url: str
    final_url: str | None = None
    pages: list[Page] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)  # homepage response headers
    notes: list[str] = field(default_factory=list)

    @property
    def ok_pages(self) -> list[Page]:
        return [p for p in self.pages if p.status_code == 200 and p.text]


def looks_js_rendered(page: Page) -> bool:
    """Little readable text but lots of scripts: content is probably built by JavaScript."""
    return len(page.text) < 800 and page.html.count("<script") > 3


def normalise_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def html_to_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "template"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = (re.sub(r"\s+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def parse_page(url: str, kind: str, status: int, html: str) -> Page:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else None
    links: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = urldefrag(urljoin(url, a["href"]))[0]
        links.append((href, a.get_text(" ", strip=True)))
    return Page(url=url, kind=kind, status_code=status, html=html, title=title,
                text=html_to_text(soup), links=links)


def _same_site(a: str, b: str) -> bool:
    ha, hb = urlparse(a).netloc.lower(), urlparse(b).netloc.lower()
    strip = lambda h: h[4:] if h.startswith("www.") else h  # noqa: E731
    return strip(ha) == strip(hb)


def _path_matches(path: str, keywords: tuple[str, ...]) -> bool:
    # A path segment that is, or starts with, the keyword: /about, /about-us, /investors
    return any(re.search(rf"/{re.escape(k)}[a-z]*(?:[-_/.]|$)", path) for k in keywords)


def _path_kind(path: str) -> str | None:
    """The page kind a URL's path clearly names, if any (/contact-us -> contact)."""
    return next((kind for kind, kws in PAGE_KINDS.items() if _path_matches(path, kws)), None)


def _match_score(path: str, text: str, keywords: tuple[str, ...]) -> int | None:
    """Lower is better; None means the link isn't this kind of page."""
    if _path_matches(path, keywords):
        return 0
    label = text.lower()
    if 0 < len(label.split()) <= 4:  # short nav labels only, not promotional blurbs
        for k in keywords:
            if re.search(rf"\b{re.escape(k.replace('-', ' '))}", label):
                return 100
    return None


def choose_pages(home: Page, max_extra: int) -> list[tuple[str, str]]:
    """Pick the best link for each page kind from the homepage's links."""
    chosen: list[tuple[str, str]] = []
    seen = {_path_key(home.url)}
    for kind, keywords in PAGE_KINDS.items():
        if len(chosen) >= max_extra:
            break
        best: tuple[int, str] | None = None
        for href, text in home.links:
            if not href.startswith("http") or not _same_site(href, home.url):
                continue
            path = urlparse(href).path.lower() or "/"
            if _SKIP_EXT.search(path) or _path_key(href) in seen:
                continue
            if LEGAL_RE.search(path) or LEGAL_RE.search(text):
                continue
            match = _match_score(path, text, keywords)
            if match is None:
                continue
            # A label can't overrule an address that clearly names another kind:
            # a link to /contact-us labelled "About Planlogic" is still the contact page.
            path_kind = _path_kind(path)
            if match > 0 and path_kind is not None and path_kind != kind:
                continue
            # Then prefer short, shallow paths (".../about" over ".../about/team/jane").
            score = match + path.count("/") * 10 + len(path)
            if best is None or score < best[0]:
                best = (score, href)
        if best:
            chosen.append((best[1], kind))
            seen.add(_path_key(best[1]))
    return chosen


def _path_key(url: str) -> str:
    """Same-site identity of a page: path only, case-insensitive, no trailing slash."""
    return urlparse(url).path.lower().rstrip("/") or "/"


def menu_links(home: Page, taken: set[str], limit: int) -> list[str]:
    """Other homepage links, in the order they appear (menus come first), to fill
    page slots the keyword search left unused. Skips legal and utility pages,
    deep links (e.g. individual blog posts) and other-language versions."""
    picked: list[str] = []
    seen = set(taken) | {_path_key(home.url)}
    for href, text in home.links:
        if len(picked) >= limit:
            break
        if not href.startswith("http") or not _same_site(href, home.url):
            continue
        path = urlparse(href).path or "/"
        key = _path_key(href)
        if key in seen or _SKIP_EXT.search(path):
            continue
        if LEGAL_RE.search(path) or LEGAL_RE.search(text) or _UTILITY_RE.search(path):
            continue
        if key.count("/") > 2 or _LANGUAGE_PREFIX_RE.match(path):
            continue  # deeper than /section/page, or a translated copy
        seen.add(key)
        picked.append(href)
    return picked


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
                        and not _NOT_FOUND_RE.search(page.title or "")):
                    result.pages.append(page)
                    break

        # Still room: take the homepage's other menu links (e.g. /eos/ for a product
        # named on the menu, which no keyword would match).
        room = max_pages - len(result.pages)
        if room > 0:
            taken = {_path_key(p.url) for p in result.pages} | {_path_key(u) for u, _ in chosen}
            for url in menu_links(home, taken, room):
                page = self.fetch(url, "other", result.notes)
                if page:
                    result.pages.append(page)
        return result

    def probe(self, url: str) -> Page | None:
        """Fetch a single page quietly (used for website discovery)."""
        notes: list[str] = []
        return self.fetch(normalise_url(url), "home", notes, allow_render=False)
