"""Small shared helpers: GET or POST an API resource through the page cache."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx

from ..schema import utcnow

USER_AGENT = "Mozilla/5.0 (compatible; informant/0.1; personal company research)"


def api_client(timeout: float = 30.0, headers: dict | None = None) -> httpx.Client:
    """An HTTP client for public APIs, identifying the program (no personal details)."""
    return httpx.Client(timeout=timeout, follow_redirects=True,
                        headers={"User-Agent": USER_AGENT, **(headers or {})})


def cached_post_json(http: httpx.Client, cache, url: str, body: dict,
                     max_age: timedelta | None = None) -> str:
    """POST a JSON body and return the response text, cached under the URL + body."""
    key = f"{url}#post={json.dumps(body, sort_keys=True)}"
    if cache is not None:
        hit = cache.get(key, max_age=max_age)
        if hit is not None and hit.status_code == 200:
            return hit.html
    resp = http.post(url, json=body)
    resp.raise_for_status()
    if cache is not None:
        cache.put(key, final_url=url, status_code=200, html=resp.text, headers={},
                  rendered=False, fetched_at=utcnow())
    return resp.text


def cached_get(http: httpx.Client, cache, url: str, params: dict | None = None,
               max_age: timedelta | None = None) -> str:
    """Return the response body, reusing a cached copy younger than `max_age`
    (default: the cache's own lifetime). Raises httpx errors on failure; failed
    responses are never cached."""
    full = str(httpx.URL(url, params=params or {}))
    if cache is not None:
        hit = cache.get(full, max_age=max_age)
        if hit is not None and hit.status_code == 200:
            return hit.html
    resp = http.get(url, params=params)
    resp.raise_for_status()
    if cache is not None:
        cache.put(full, final_url=full, status_code=200, html=resp.text, headers={},
                  rendered=False, fetched_at=utcnow())
    return resp.text
