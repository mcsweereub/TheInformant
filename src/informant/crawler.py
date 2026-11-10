"""Polite, small-scale website crawler.

Fetches a company's homepage plus a handful of key pages (about, products,
careers, contact, news). Deliberately conservative:
  * obeys robots.txt (and its Crawl-delay), identifies itself honestly
  * one request at a time with a delay, same host only, HTML only
  * no login/paywall/CAPTCHA bypass: if a site blocks us we note it and move on
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urldefrag, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

USER_AGENT = "Mozilla/5.0 (compatible; informant/0.1; personal company research)"
MAX_BYTES = 3_000_000

# Page kinds we look for, in priority order, with URL/link-text keywords.
PAGE_KINDS: dict[str, tuple[str, ...]] = {
    "about": ("about", "who-we-are", "our-story", "company", "our-company"),
    "products": ("product", "service", "solution", "what-we-do", "platform", "shop", "menu"),
    "careers": ("career", "jobs", "join-us", "work-with-us", "vacancies", "recruit"),
    "contact": ("contact", "find-us", "locations", "offices", "stores"),
    "news": ("news", "press", "media", "blog", "insights"),
    "investors": ("investor", "results", "annual-report"),
}

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


def choose_pages(home: Page, max_extra: int) -> list[tuple[str, str]]:
    """Pick the best link for each page kind from the homepage's links."""
    chosen: list[tuple[str, str]] = []
    seen = {home.url.rstrip("/")}
    for kind, keywords in PAGE_KINDS.items():
        best: tuple[int, str] | None = None
        for href, text in home.links:
            if not href.startswith("http") or not _same_site(href, home.url):
                continue
            path = urlparse(href).path.lower()
            if _SKIP_EXT.search(path) or href.rstrip("/") in seen:
                continue
            haystack = f"{path} {text.lower()}"
            if not any(k in haystack for k in keywords):
                continue
            # Prefer short, shallow paths (".../about" over ".../about/team/jane").
            score = path.count("/") * 10 + len(path)
            if best is None or score < best[0]:
                best = (score, href)
        if best:
            chosen.append((best[1], kind))
            seen.add(best[1].rstrip("/"))
        if len(chosen) >= max_extra:
            break
    return chosen


class Crawler:
    def __init__(self, *, delay: float = 1.0, timeout: float = 15.0,
                 client: httpx.Client | None = None) -> None:
        self.delay = delay
        self._http = client or httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        )
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_request = 0.0

    def close(self) -> None:
        self._http.close()

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

    def fetch(self, url: str, kind: str, notes: list[str]) -> Page | None:
        if not self.allowed(url):
            notes.append(f"robots.txt disallows {url}; skipped")
            return None
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
        if len(home.text) < 800 and home.html.count("<script") > 3:
            result.notes.append(
                "homepage has very little text and lots of scripts: probably "
                "JavaScript-rendered, so content may be missing (no headless browser yet)"
            )

        for url, kind in choose_pages(home, max_extra=max_pages - 1):
            page = self.fetch(url, kind, result.notes)
            if page:
                result.pages.append(page)
        return result

    def probe(self, url: str) -> Page | None:
        """Fetch a single page quietly (used for website discovery)."""
        notes: list[str] = []
        return self.fetch(normalise_url(url), "home", notes)
