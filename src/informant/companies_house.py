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

from .schema import (
    Classification,
    Compliance,
    Filing,
    Filings,
    Identity,
    Location,
    Officer,
    People,
    PreviousName,
    SicCode,
    SignificantControl,
    Source,
)

BASE_URL = "https://api.company-information.service.gov.uk"
PUBLIC_URL = "https://find-and-update.company-information.service.gov.uk"
DOCUMENT_URL = "https://find-and-update.company-information.service.gov.uk"

# e.g. 00445790, SC123456, OC301234, NI012345
COMPANY_NUMBER_RE = re.compile(r"^(?:[A-Z]{2}\d{6}|\d{8})$")


def normalise_company_number(value: str) -> str | None:
    """Return a canonical company number, or None if `value` isn't one."""
    v = value.strip().upper().replace(" ", "")
    if v.isdigit() and len(v) < 8:
        v = v.zfill(8)
    return v if COMPANY_NUMBER_RE.match(v) else None


class CompaniesHouseError(RuntimeError):
    pass


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
            raise CompaniesHouseError(
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

    def _get(self, path: str, *, allow_404: bool = False, **params: Any) -> dict | None:
        for attempt in range(self._max_retries + 1):
            resp = self._http.get(path, params=params or None)
            if resp.status_code == 429 and attempt < self._max_retries:
                wait = float(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
                time.sleep(min(wait, 60))
                continue
            if resp.status_code == 404 and allow_404:
                return None
            if resp.status_code == 401:
                raise CompaniesHouseError("Companies House rejected the API key (401).")
            if resp.is_error:
                raise CompaniesHouseError(
                    f"GET {path} failed: {resp.status_code} {resp.text[:200]}"
                )
            return resp.json()
        raise CompaniesHouseError(f"GET {path} still rate limited after retries.")

    def _paged(self, path: str, *, limit: int, allow_404: bool = False) -> list[dict]:
        items: list[dict] = []
        start = 0
        while len(items) < limit:
            page = self._get(
                path,
                allow_404=allow_404,
                items_per_page=min(100, limit - len(items)),
                start_index=start,
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
            raise CompaniesHouseError(f"No company found with number {number}.")
        return data

    def officers(self, number: str, limit: int = 200) -> list[dict]:
        return self._paged(f"/company/{number}/officers", limit=limit, allow_404=True)

    def pscs(self, number: str, limit: int = 100) -> list[dict]:
        return self._paged(
            f"/company/{number}/persons-with-significant-control",
            limit=limit,
            allow_404=True,
        )

    def filing_history(self, number: str, limit: int = 25) -> list[dict]:
        return self._paged(f"/company/{number}/filing-history", limit=limit, allow_404=True)


# --- mapping raw API JSON onto the schema ---------------------------------


@dataclass
class CompaniesHouseData:
    identity: Identity
    classification: Classification
    locations: list[Location]
    people: People
    filings: Filings


def _source(number: str, suffix: str = "") -> Source:
    return Source(name="companies_house", url=f"{PUBLIC_URL}/company/{number}{suffix}")


def _filing_description(item: dict) -> str | None:
    """Filing descriptions are template keys; make them readable."""
    desc = item.get("description")
    if not desc:
        return None
    values = item.get("description_values") or {}
    if "description" in values:  # legacy filings carry free text here
        return values["description"]
    text = desc.replace("-", " ")
    extras = [str(v) for k, v in values.items() if k in ("made_up_date", "officer_name")]
    return f"{text} ({', '.join(extras)})" if extras else text


def map_companies_house(
    profile: dict,
    officers: list[dict],
    pscs: list[dict],
    filings: list[dict],
) -> CompaniesHouseData:
    number = profile["company_number"]

    identity = Identity(
        name=profile.get("company_name", ""),
        company_number=number,
        status=profile.get("company_status"),
        company_type=profile.get("type"),
        jurisdiction=profile.get("jurisdiction"),
        incorporated_on=profile.get("date_of_creation"),
        dissolved_on=profile.get("date_of_cessation"),
        previous_names=[
            PreviousName(
                name=p.get("name", ""),
                effective_from=p.get("effective_from"),
                ceased_on=p.get("ceased_on"),
            )
            for p in profile.get("previous_company_names") or []
        ],
        has_charges=profile.get("has_charges"),
        has_insolvency_history=profile.get("has_insolvency_history"),
        sources=[_source(number)],
    )

    classification = Classification(
        sic_codes=[SicCode(code=c) for c in profile.get("sic_codes") or []],
        sources=[_source(number)],
    )

    locations: list[Location] = []
    roa = profile.get("registered_office_address")
    if roa:
        lines = [roa.get(k) for k in ("premises", "address_line_1", "address_line_2")]
        locations.append(
            Location(
                kind="registered_office",
                address_lines=[l for l in lines if l],
                locality=roa.get("locality"),
                region=roa.get("region"),
                postal_code=roa.get("postal_code"),
                country=roa.get("country"),
                sources=[_source(number)],
            )
        )

    people = People(
        officers=[
            Officer(
                name=o.get("name", ""),
                role=o.get("officer_role", ""),
                appointed_on=o.get("appointed_on"),
                resigned_on=o.get("resigned_on"),
                occupation=o.get("occupation"),
                nationality=o.get("nationality"),
                country_of_residence=o.get("country_of_residence"),
            )
            for o in officers
        ],
        significant_control=[
            SignificantControl(
                name=p.get("name", ""),
                kind=p.get("kind", ""),
                natures_of_control=p.get("natures_of_control") or [],
                notified_on=p.get("notified_on"),
                ceased_on=p.get("ceased_on"),
                nationality=p.get("nationality"),
                country_of_residence=p.get("country_of_residence"),
            )
            for p in pscs
        ],
        sources=[_source(number, "/officers"), _source(number, "/persons-with-significant-control")],
    )

    accounts = profile.get("accounts") or {}
    last_accounts = accounts.get("last_accounts") or {}
    conf = profile.get("confirmation_statement") or {}
    filings_section = Filings(
        compliance=Compliance(
            accounts_next_due=accounts.get("next_due"),
            accounts_overdue=accounts.get("overdue"),
            last_accounts_made_up_to=last_accounts.get("made_up_to"),
            last_accounts_type=last_accounts.get("type"),
            confirmation_statement_next_due=conf.get("next_due"),
            confirmation_statement_overdue=conf.get("overdue"),
        ),
        recent=[
            Filing(
                date=f.get("date"),
                category=f.get("category"),
                type=f.get("type"),
                description=_filing_description(f),
                document_url=(
                    f"{DOCUMENT_URL}/company/{number}/filing-history/{f['transaction_id']}"
                    "/document?format=pdf&download=0"
                    if f.get("transaction_id")
                    else None
                ),
            )
            for f in filings
        ],
        sources=[_source(number, "/filing-history")],
    )

    return CompaniesHouseData(identity, classification, locations, people, filings_section)


def fetch_company(client: CompaniesHouseClient, number: str, filings_limit: int = 25) -> CompaniesHouseData:
    return map_companies_house(
        client.profile(number),
        client.officers(number),
        client.pscs(number),
        client.filing_history(number, limit=filings_limit),
    )
