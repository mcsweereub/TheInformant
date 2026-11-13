"""Which pages of a company website to read.

From the homepage's links, pick the best page of each kind (about, products,
investors, customers, careers, contact, news), skip legal boilerplate, and fill
any spare slots with the homepage's other menu links.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from .parse import Page, path_key, same_site

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
# Titles of "page not found" pages served with status 200.
NOT_FOUND_RE = re.compile(r"\b(404|page not found|can.t find (that|this) page)\b", re.I)
# Links that never make useful research pages when filling spare slots.
_UTILITY_RE = re.compile(
    r"log-?in|sign-?in|sign-?up|register|account|basket|cart|checkout|search|wp-admin|"
    r"wp-login|feed|/tag/|/category/|/author/",
    re.I,
)
_LANGUAGE_PREFIX_RE = re.compile(r"^/[a-z]{2}(?:-[a-z]{2})?(?:/|$)", re.I)  # /fr/, /en-gb/
_SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|svg|webp|zip|docx?|xlsx?|pptx?|mp4|mp3)$", re.I)


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


def sitemap_candidates(urls: list[str], home_url: str) -> list[tuple[str, str]]:
    """Sitemap URLs worth considering as key pages: same site, shallow (not individual
    posts or products), not a translated copy. Returned as (url, "") link pairs."""
    links = []
    for url in urls:
        if not url.startswith("http") or not same_site(url, home_url):
            continue
        path = urlparse(url).path or "/"
        # Top-level pages only: deeper sitemap entries are mostly individual posts,
        # whose addresses can start with a keyword (/blog/contact-us-in-sign-language).
        if path_key(url).count("/") > 1 or _LANGUAGE_PREFIX_RE.match(path):
            continue
        links.append((url, ""))
    return links


def choose_pages(home: Page, max_extra: int, *, links: list[tuple[str, str]] | None = None,
                 kinds: list[str] | None = None, taken: set[str] | None = None) -> list[tuple[str, str]]:
    """Pick the best link for each page kind.

    By default the candidates are the homepage's own links. Pass `links` to choose
    from another list (e.g. the sitemap), `kinds` to look only for some page kinds,
    and `taken` (path keys) for pages already chosen.
    """
    chosen: list[tuple[str, str]] = []
    seen = {path_key(home.url)} | set(taken or ())
    candidates = home.links if links is None else links
    for kind, keywords in PAGE_KINDS.items():
        if kinds is not None and kind not in kinds:
            continue
        if len(chosen) >= max_extra:
            break
        best: tuple[int, str] | None = None
        for href, text in candidates:
            if not href.startswith("http") or not same_site(href, home.url):
                continue
            path = urlparse(href).path.lower() or "/"
            if _SKIP_EXT.search(path) or path_key(href) in seen:
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
            seen.add(path_key(best[1]))
    return chosen


def menu_links(home: Page, taken: set[str], limit: int) -> list[str]:
    """Other homepage links, in the order they appear (menus come first), to fill
    page slots the keyword search left unused. Skips legal and utility pages,
    deep links (e.g. individual blog posts) and other-language versions."""
    picked: list[str] = []
    seen = set(taken) | {path_key(home.url)}
    for href, text in home.links:
        if len(picked) >= limit:
            break
        if not href.startswith("http") or not same_site(href, home.url):
            continue
        path = urlparse(href).path or "/"
        key = path_key(href)
        if key in seen or _SKIP_EXT.search(path):
            continue
        if LEGAL_RE.search(path) or LEGAL_RE.search(text) or _UTILITY_RE.search(path):
            continue
        if key.count("/") > 2 or _LANGUAGE_PREFIX_RE.match(path):
            continue  # deeper than /section/page, or a translated copy
        seen.add(key)
        picked.append(href)
    return picked
