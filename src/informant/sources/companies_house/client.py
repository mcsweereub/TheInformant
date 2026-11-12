"""Companies House public data API client.

Docs: https://developer-specs.company-information.service.gov.uk/
Auth: HTTP basic auth, API key as username, empty password.
Rate limit: 600 requests per 5 minutes per key.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ...errors import ConfigError, NotFoundError, UpstreamError

BASE_URL = "https://api.company-information.service.gov.uk"
PUBLIC_URL = "https://find-and-update.company-information.service.gov.uk"
DOCUMENT_API = "https://document-api.company-information.service.gov.uk"
MAX_DOCUMENT_BYTES = 20_000_000

# e.g. 00445790, SC123456, OC301234, NI012345
COMPANY_NUMBER_RE = re.compile(r"^(?:[A-Z]{2}\d{6}|\d{8})$")


def normalise_company_number(value: str) -> str | None:
    """Return a canonical company number, or None if `value` isn't one."""
    v = value.strip().upper().replace(" ", "")
    if v.isdigit() and len(v) < 8:
        v = v.zfill(8)
    return v if COMPANY_NUMBER_RE.match(v) else None


class CompaniesHouseError(UpstreamError):
    """Companies House request failed (network, server error, rate limit)."""


class CompaniesHouseAuthError(CompaniesHouseError, ConfigError):
    """API key missing or rejected."""

    code = ConfigError.code
    exit_code = ConfigError.exit_code


class CompanyNotFoundError(CompaniesHouseError, NotFoundError):
    code = NotFoundError.code
    exit_code = NotFoundError.exit_code


@dataclass
class SearchHit:
    company_number: str
    title: str
    status: str | None
    address: str | None
    date_of_creation: str | None


class CompaniesHouseClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        timeout: float = 20.0,
        max_retries: int = 3,
    ) -> None:
        if not api_key:
            raise CompaniesHouseAuthError(
                "COMPANIES_HOUSE_API_KEY is not set. Get a free key at "
                "https://developer.company-information.service.gov.uk/"
            )
        self._max_retries = max_retries
        self._http = httpx.Client(
            base_url=base_url,
            auth=(api_key, ""),
            timeout=timeout,
            headers={"User-Agent": "informant/0.1 (personal research)"},
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> CompaniesHouseClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- low level --------------------------------------------------------

    def _request(self, path: str, *, params: dict | None = None,
                 headers: dict | None = None) -> httpx.Response:
        """GET with retry on rate limiting. `path` may be a full URL (document API)."""
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._http.get(path, params=params or None, headers=headers)
            except httpx.HTTPError as e:
                raise CompaniesHouseError(
                    f"Could not reach Companies House ({type(e).__name__})."
                ) from e
            if resp.status_code == 429 and attempt < self._max_retries:
                wait = float(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
                time.sleep(min(wait, 60))
                continue
            if resp.status_code == 401:
                raise CompaniesHouseAuthError("Companies House rejected the API key (401).")
            return resp
        raise CompaniesHouseError(f"GET {path} still rate limited after retries.")

    def _get(self, path: str, *, allow_404: bool = False, **params: Any) -> dict | None:
        resp = self._request(path, params=params)
        if resp.status_code == 404 and allow_404:
            return None
        if resp.is_error:
            raise CompaniesHouseError(f"GET {path} failed: {resp.status_code} {resp.text[:200]}")
        return resp.json()

    def _paged(self, path: str, *, limit: int, allow_404: bool = False, **params: Any) -> list[dict]:
        items: list[dict] = []
        start = 0
        while len(items) < limit:
            page = self._get(
                path,
                allow_404=allow_404,
                items_per_page=min(100, limit - len(items)),
                start_index=start,
                **params,
            )
            if not page:
                break
            batch = page.get("items") or []
            items.extend(batch)
            start += len(batch)
            total = page.get("total_results") or page.get("total_count") or 0
            if not batch or start >= total:
                break
        return items[:limit]

    # --- endpoints --------------------------------------------------------

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        data = self._get("/search/companies", q=query, items_per_page=limit) or {}
        return [
            SearchHit(
                company_number=i.get("company_number", ""),
                title=i.get("title", ""),
                status=i.get("company_status"),
                address=i.get("address_snippet"),
                date_of_creation=i.get("date_of_creation"),
            )
            for i in data.get("items") or []
        ]

    def profile(self, number: str) -> dict:
        data = self._get(f"/company/{number}", allow_404=True)
        if data is None:
            raise CompanyNotFoundError(f"No company found with number {number}.")
        return data

    def officers(self, number: str, limit: int = 200) -> list[dict]:
        return self._paged(f"/company/{number}/officers", limit=limit, allow_404=True)

    def pscs(self, number: str, limit: int = 100) -> list[dict]:
        return self._paged(
            f"/company/{number}/persons-with-significant-control",
            limit=limit,
            allow_404=True,
        )

    def filing_history(self, number: str, limit: int = 25, category: str | None = None) -> list[dict]:
        """Newest first. `category` filters, e.g. "accounts", "capital"."""
        params = {"category": category} if category else {}
        return self._paged(f"/company/{number}/filing-history", limit=limit, allow_404=True, **params)

    def charges(self, number: str) -> dict:
        """Mortgages and charges: {"items": [...], "total_count", "satisfied_count", ...}."""
        return self._get(f"/company/{number}/charges", allow_404=True, items_per_page=100) or {}

    def appointments(self, path: str, limit: int = 100) -> tuple[list[dict], int]:
        """An officer's appointments across all companies, as (items, total count).
        `path` comes from an officer record: links.officer.appointments."""
        first = self._get(path, allow_404=True, items_per_page=50) or {}
        items = list(first.get("items") or [])
        total = first.get("total_results") or len(items)
        while len(items) < min(total, limit):
            page = self._get(path, allow_404=True, items_per_page=50, start_index=len(items)) or {}
            batch = page.get("items") or []
            if not batch:
                break
            items.extend(batch)
        return items[:limit], total

    # --- filed documents ----------------------------------------------------

    def document_formats(self, metadata_url: str) -> dict[str, Any]:
        """Formats a filed document is available in, e.g. {"application/pdf": {...},
        "application/xhtml+xml": {...}}. `metadata_url` is links.document_metadata."""
        resp = self._request(metadata_url)
        if resp.status_code == 404:
            return {}
        if resp.is_error:
            raise CompaniesHouseError(f"document metadata failed: {resp.status_code}")
        return resp.json().get("resources") or {}

    def document_content(self, metadata_url: str, content_type: str) -> bytes:
        """Download a filed document in the given format."""
        resp = self._request(f"{metadata_url}/content", headers={"Accept": content_type})
        if resp.status_code in (301, 302, 303, 307, 308):
            # The API redirects to a pre-signed storage URL, which rejects requests
            # that also carry our API key, so follow it with a plain client.
            try:
                resp = httpx.get(resp.headers["location"], timeout=60, follow_redirects=True)
            except httpx.HTTPError as e:
                raise CompaniesHouseError(f"document download failed ({type(e).__name__})") from e
        if resp.is_error:
            raise CompaniesHouseError(f"document download failed: {resp.status_code}")
        if len(resp.content) > MAX_DOCUMENT_BYTES:
            raise CompaniesHouseError("document too large to process")
        return resp.content
