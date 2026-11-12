"""Page data types and HTML parsing: raw HTML -> readable text, title and links."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup


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
    fetched_at: datetime | None = None  # when it was really downloaded
    from_cache: bool = False  # served from the local cache this run


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


def same_site(a: str, b: str) -> bool:
    """True if two URLs are on the same host, ignoring a leading www."""
    ha, hb = urlparse(a).netloc.lower(), urlparse(b).netloc.lower()
    strip = lambda h: h[4:] if h.startswith("www.") else h  # noqa: E731
    return strip(ha) == strip(hb)


def path_key(url: str) -> str:
    """Same-site identity of a page: path only, case-insensitive, no trailing slash."""
    return urlparse(url).path.lower().rstrip("/") or "/"
