"""Recent news about a company: headlines, links, publishers and dates.

Providers (all free; results are merged and de-duplicated):
    bing      Bing News RSS feed. No key. Its robots.txt permits the feed.
    gdelt     GDELT DOC API: an open news-search API. No key; limited to one
              request every 5 seconds, and sometimes unavailable.
    guardian  The Guardian's official Open Platform API. Needs GUARDIAN_API_KEY.

Google News RSS is deliberately not used: its robots.txt disallows the feed.

Only headline, link, publisher, date and the feed's short snippet are kept;
article text is never fetched. A headline is kept only if it names the company
(by its registered name or its brand, taken from the website's domain).
"""

from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlparse

import httpx

from ..schema import utcnow
from .website.extract import core_name, site_host

USER_AGENT = "Mozilla/5.0 (compatible; informant/0.1; personal company research)"
BING_URL = "https://www.bing.com/news/search"
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GUARDIAN_URL = "https://content.guardianapis.com/search"
FEED_MAX_AGE = timedelta(hours=6)  # how long a fetched feed is reused from the cache
DEFAULT_DAYS = 90
DEFAULT_LIMIT = 20

# Brand names that are also everyday words need a stricter (capitalised) match.
_COMMON_WORDS = {"next", "boots", "shell", "apple", "orange", "three", "sky", "virgin", "target",
                 "first", "one", "now", "go", "pure", "simply", "smart", "home", "hive", "bulb",
                 "match", "river", "island", "monday", "friday", "energy", "group", "capital"}
# First words of registered names that say nothing on their own ("Royal Mail").
_GENERIC_FIRST_WORDS = {
    "royal", "british", "national", "united", "global", "international", "london", "general",
    "standard", "city", "north", "south", "east", "west", "northern", "southern", "eastern",
    "western", "great", "new", "best", "premier", "advanced", "modern", "total", "prime",
    "central", "metro", "euro", "asia", "america", "american", "english", "scottish", "welsh",
    "irish", "digital", "direct", "pro", "star", "blue", "green", "red", "black", "white",
}
_TAG = re.compile(r"<[^>]+>")


@dataclass
class Article:
    title: str
    url: str
    published_at: datetime | None = None
    publisher: str | None = None
    summary: str | None = None
    provider: str = ""


@dataclass
class NewsResult:
    articles: list[Article] = field(default_factory=list)
    names: list[str] = field(default_factory=list)  # what was searched for
    providers: list[str] = field(default_factory=list)  # providers that answered
    notes: list[str] = field(default_factory=list)


def search_names(company_name: str, website: str | None) -> list[str]:
    """Names to search for: the brand (from the website's domain) and the
    registered name without its legal suffix. 'MONZO BANK LIMITED' + monzo.com
    -> ['monzo', 'monzo bank']."""
    names: list[str] = []
    if website:
        label = site_host(website).split(".")[0].replace("-", " ").strip()
        if len(label) >= 3:
            names.append(label)
    core = core_name(company_name)
    if core and core not in names:
        names.append(core)
    # Headlines use the brand ("Monzo"), not the registered name ("Monzo Bank"), so add
    # a distinctive first word on its own. Generic first words would match everything.
    first = core.split()[0] if core and len(core.split()) > 1 else ""
    if (len(first) >= 4 and first not in _GENERIC_FIRST_WORDS and first not in _COMMON_WORDS
            and not any(first == n.split()[0] for n in names if len(n.split()) == 1)):
        names.insert(0 if not website else len(names), first)
    return names


def mentions(title: str, names: list[str]) -> bool:
    """True if the headline names the company. Everyday-word brands (Next, Shell)
    must appear capitalised, which removes most accidental matches."""
    for name in names:
        words = name.split()
        strict = name in _COMMON_WORDS or len(name) <= 3
        if strict:
            words = [w.capitalize() for w in words]
        pattern = r"\b" + r"\s+".join(re.escape(w) for w in words) + r"(?:'s|’s)?\b"
        if re.search(pattern, title, 0 if strict else re.I):
            return True
    return False


def _clean(text: str | None) -> str | None:
    if not text:
        return None
    text = " ".join(_TAG.sub(" ", text).split())
    return text or None


def _get(http: httpx.Client, cache, url: str, params: dict) -> str:
    """GET through the page cache (feeds are reused for a few hours)."""
    full = str(httpx.URL(url, params=params))
    if cache is not None:
        hit = cache.get(full, max_age=FEED_MAX_AGE)
        if hit is not None and hit.status_code == 200:
            return hit.html
    resp = http.get(url, params=params)
    resp.raise_for_status()
    if cache is not None:
        cache.put(full, final_url=full, status_code=200, html=resp.text, headers={},
                  rendered=False, fetched_at=utcnow())
    return resp.text


# --- providers ------------------------------------------------------------------


def fetch_bing(http: httpx.Client, cache, names: list[str]) -> list[Article]:
    # The feed doesn't support OR (it returns nothing), so ask once per name.
    articles: list[Article] = []
    for name in names[:2]:
        xml = _get(http, cache, BING_URL, {"q": f'"{name}"', "format": "rss", "mkt": "en-GB"})
        articles.extend(_parse_bing(xml))
    return articles


def _parse_bing(xml: str) -> list[Article]:
    articles = []
    for item in ET.fromstring(xml).iter("item"):
        fields = {child.tag.split("}")[-1].lower(): (child.text or "") for child in item}
        link = fields.get("link", "")
        # Bing wraps each link in a click-tracking redirect; the real address is in ?url=
        target = parse_qs(urlparse(link).query).get("url", [link])[0]
        try:
            published = parsedate_to_datetime(fields["pubdate"]) if fields.get("pubdate") else None
        except (TypeError, ValueError):
            published = None
        title = _clean(fields.get("title"))
        if title and target.startswith("http"):
            articles.append(Article(title=title, url=target, published_at=published,
                                    publisher=_clean(fields.get("source")),
                                    summary=_clean(fields.get("description")), provider="bing"))
    return articles


def fetch_gdelt(http: httpx.Client, cache, names: list[str], days: int) -> list[Article]:
    query = "(" + " OR ".join(f'"{n}"' for n in names) + ")" if len(names) > 1 else f'"{names[0]}"'
    params = {"query": f"{query} sourcelang:english", "mode": "artlist", "format": "json",
              "maxrecords": 50, "timespan": f"{days}d", "sort": "datedesc"}
    try:
        text = _get(http, cache, GDELT_URL, params)
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 429:
            raise
        time.sleep(6)  # the API allows one request every 5 seconds
        text = _get(http, cache, GDELT_URL, params)
    import json

    articles = []
    for a in (json.loads(text).get("articles") or []):
        try:
            published = datetime.strptime(a.get("seendate", ""), "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc)
        except ValueError:
            published = None
        title = _clean(a.get("title"))
        if title and str(a.get("url", "")).startswith("http"):
            articles.append(Article(title=title, url=a["url"], published_at=published,
                                    publisher=a.get("domain"), provider="gdelt"))
    return articles


def fetch_guardian(http: httpx.Client, cache, names: list[str], days: int, key: str) -> list[Article]:
    since = (utcnow() - timedelta(days=days)).date().isoformat()
    params = {"q": " OR ".join(f'"{n}"' for n in names), "from-date": since,
              "order-by": "newest", "page-size": 30, "show-fields": "trailText", "api-key": key}
    import json

    data = json.loads(_get(http, cache, GUARDIAN_URL, params))
    articles = []
    for r in (data.get("response") or {}).get("results") or []:
        try:
            published = datetime.fromisoformat(r["webPublicationDate"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            published = None
        if r.get("webTitle") and r.get("webUrl"):
            articles.append(Article(title=_clean(r["webTitle"]), url=r["webUrl"],
                                    published_at=published, publisher="The Guardian",
                                    summary=_clean((r.get("fields") or {}).get("trailText")),
                                    provider="guardian"))
    return articles


# --- combining --------------------------------------------------------------------


def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())[:70]


def fetch_news(company_name: str, website: str | None = None, *, days: int = DEFAULT_DAYS,
               limit: int = DEFAULT_LIMIT, cache=None, http: httpx.Client | None = None,
               allowed=None) -> NewsResult:
    """Search every available provider. `allowed(url) -> bool` is the robots.txt
    check (the crawler's); a provider that fails is noted and skipped."""
    result = NewsResult(names=search_names(company_name, website))
    if not result.names:
        result.notes.append("no usable name to search news for")
        return result

    own = http is None
    http = http or httpx.Client(timeout=25, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    guardian_key = os.environ.get("GUARDIAN_API_KEY", "").strip()
    found: list[Article] = []
    try:
        providers = [("bing", lambda: fetch_bing(http, cache, result.names)),
                     ("gdelt", lambda: fetch_gdelt(http, cache, result.names, days))]
        if guardian_key:
            providers.append(("guardian",
                              lambda: fetch_guardian(http, cache, result.names, days, guardian_key)))
        for name, fetch in providers:
            if name == "bing" and allowed is not None and not allowed(f"{BING_URL}?q=x&format=rss"):
                result.notes.append("Bing News: its robots.txt no longer permits the feed; skipped")
                continue
            try:
                found.extend(fetch())
                result.providers.append(name)
            except (httpx.HTTPError, ET.ParseError, ValueError) as e:
                result.notes.append(f"{name} news search failed ({type(e).__name__}); skipped")
    finally:
        if own:
            http.close()

    cutoff = utcnow() - timedelta(days=days)
    seen: set[str] = set()
    kept: list[Article] = []
    off_topic = 0
    # Providers with snippets first, so a duplicate keeps the richer copy.
    for a in sorted(found, key=lambda a: a.summary is None):
        if a.published_at is not None and a.published_at < cutoff:
            continue
        if not mentions(a.title, result.names):
            off_topic += 1
            continue
        key = _title_key(a.title)
        if key in seen or a.url in seen:
            continue
        seen.update((key, a.url))
        kept.append(a)
    kept.sort(key=lambda a: a.published_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    if off_topic:
        result.notes.append(f"left out {off_topic} result(s) whose headline doesn't name the company")
    if len(kept) > limit:
        result.notes.append(f"showing the {limit} most recent of {len(kept)} articles")
    result.articles = kept[:limit]
    return result
