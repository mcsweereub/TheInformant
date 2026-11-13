"""Small shared helper: GET a JSON or text resource through the page cache."""

from __future__ import annotations

from datetime import timedelta

import httpx

from ..schema import utcnow


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
