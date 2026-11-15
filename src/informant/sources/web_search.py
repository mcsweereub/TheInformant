"""Web search through Tavily's official API (free tier: about 1,000 searches a month).

Used only to fill gaps, so a lookup makes at most a few searches:
  * mentions: what other sites say about the company (snippets go to the LLM)
  * website: when domain guessing and Wikidata can't find the official site
  * annual report: when the company's own PDF isn't linked from its site

Every search is cached (7 days), so re-running a company costs nothing.
Needs TAVILY_API_KEY; INFORMANT_SEARCH=off disables it; INFORMANT_SEARCH_MAX caps
searches per lookup (default 4). Never scrapes search engines' result pages.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import timedelta

import httpx

from ..schema import utcnow

TAVILY_URL = "https://api.tavily.com/search"
DEFAULT_MAX_SEARCHES = 4
CACHE_MAX_AGE = timedelta(days=7)
_OFF = {"0", "off", "false", "no"}

# Never returned as mentions: personal profiles and people-finder sites (our rules:
# no profiles of individuals), and social networks generally.
PEOPLE_AND_SOCIAL_DOMAINS = [
    "linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com", "tiktok.com",
    "youtube.com", "pinterest.com", "reddit.com", "192.com", "ukphonebook.com", "peoplefinder.com",
    "spokeo.com", "truepeoplesearch.com", "whitepages.com",
    "tavily.com",  # the search provider's own auto-generated company pages
]


@dataclass
class SearchResult:
    title: str
    url: str
    content: str  # the snippet
    score: float = 0.0


@dataclass
class WebSearch:
    """A per-lookup search client with a budget. Construct with `from_env()`."""

    api_key: str
    max_searches: int = DEFAULT_MAX_SEARCHES
    cache: object | None = None
    http: httpx.Client | None = None
    used: int = 0  # real API calls made (cache hits are free)
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls, cache=None) -> WebSearch | None:
        key = os.environ.get("TAVILY_API_KEY", "").strip()
        if not key or os.environ.get("INFORMANT_SEARCH", "on").strip().lower() in _OFF:
            return None
        limit = int(os.environ.get("INFORMANT_SEARCH_MAX", DEFAULT_MAX_SEARCHES))
        return cls(api_key=key, max_searches=max(0, limit), cache=cache)

    def search(self, query: str, *, max_results: int = 8, include_domains: list[str] | None = None,
               exclude_domains: list[str] | None = None) -> list[SearchResult]:
        """Never raises: failures and an exhausted budget become notes and an empty list."""
        body = {"query": query, "search_depth": "basic", "max_results": max_results}
        if include_domains:
            body["include_domains"] = include_domains
        if exclude_domains:
            body["exclude_domains"] = exclude_domains
        key = "tavily:" + json.dumps(body, sort_keys=True)

        if self.cache is not None:
            hit = self.cache.get(key, max_age=CACHE_MAX_AGE)
            if hit is not None and hit.status_code == 200:
                return _parse(hit.html)
        if self.used >= self.max_searches:
            self.notes.append(f"search budget reached ({self.max_searches} per lookup); "
                              f"skipped: {query}")
            return []
        self.used += 1
        http = self.http or httpx.Client(timeout=30)
        try:
            resp = http.post(TAVILY_URL, json=body,
                             headers={"Authorization": f"Bearer {self.api_key}"})
        except httpx.HTTPError as e:
            self.notes.append(f"web search failed ({type(e).__name__})")
            return []
        finally:
            if self.http is None:
                http.close()
        if resp.status_code in (401, 403):
            self.notes.append("web search: Tavily rejected the API key")
            return []
        if resp.status_code == 429 or resp.status_code == 432:
            self.notes.append("web search: Tavily's free allowance or rate limit was reached")
            return []
        if resp.is_error:
            self.notes.append(f"web search failed ({resp.status_code})")
            return []
        if self.cache is not None:
            self.cache.put(key, final_url=TAVILY_URL, status_code=200, html=resp.text, headers={},
                           rendered=False, fetched_at=utcnow())
        return _parse(resp.text)


def _parse(text: str) -> list[SearchResult]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    out = []
    for r in data.get("results") or []:
        url = r.get("url") or ""
        if url.startswith("http"):
            out.append(SearchResult(title=" ".join((r.get("title") or "").split()), url=url,
                                    content=" ".join((r.get("content") or "").split()),
                                    score=float(r.get("score") or 0)))
    return out
