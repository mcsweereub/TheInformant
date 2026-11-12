"""The full lookup as one reusable function.

    from informant.pipeline import research_company
    result = research_company("00502851", progress=print)
    result.profile  # CompanyProfile

Used by the CLI, and importable by other Python programs (e.g. a GUI backend).
Raises InformantError subclasses (see errors.py) on failure.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .cache import AccountsCache, LlmCache, PageCache, cache_enabled
from .errors import ConfigError, NotFoundError
from .sources.companies_house import (
    CompaniesHouseClient,
    SearchHit,
    fetch_company,
    normalise_company_number,
)
from .sources.companies_house.details import STAGE as REGISTRY_STAGE
from .sources.companies_house.details import add_registry_details, add_risk_flags
from .sources.website.fetch import Crawler
from .progress import ProgressFn, emit
from .schema import CompanyProfile
from .sources.website.research import research_website


REGISTRY_DEPTHS = ("full", "basic")


@dataclass
class LookupResult:
    profile: CompanyProfile
    matched_by: str  # "number" or "search"
    alternatives: list[SearchHit] = field(default_factory=list)  # other search matches


def resolve_company(ch: CompaniesHouseClient, query: str,
                    progress: ProgressFn | None = None) -> tuple[str, str, list[SearchHit]]:
    """Turn a name or number into a company number. Returns (number, matched_by, alternatives)."""
    emit(progress, "resolve", "started", f"Finding {query!r}")
    number = normalise_company_number(query)
    if number is not None:
        emit(progress, "resolve", "done", number, company_number=number, matched_by="number")
        return number, "number", []
    hits = ch.search(query, limit=5)
    if not hits:
        emit(progress, "resolve", "failed", "No match")
        raise NotFoundError(f"No Companies House match for {query!r}.")
    top = hits[0]
    emit(progress, "resolve", "done", f"{top.title} ({top.company_number})",
         company_number=top.company_number, name=top.title, matched_by="search")
    return top.company_number, "search", hits[1:]


def research_company(
    query: str,
    *,
    website: str | None = None,
    web: bool = True,
    llm: bool = True,
    max_pages: int = 8,
    filings: int = 25,
    browser: str | None = None,
    llm_strategy: str | None = None,
    use_cache: bool = True,
    refresh: bool = False,
    registry: str | None = None,
    progress: ProgressFn | None = None,
    ch_client: CompaniesHouseClient | None = None,
    crawler: Crawler | None = None,
) -> LookupResult:
    """Build a CompanyProfile for a company name or number (does not save it).

    registry: "full" (also read filed accounts, share capital, charges, ownership
    and related companies) or "basic"; defaults to INFORMANT_REGISTRY or "full".
    browser: "auto" (headless browser only for JavaScript-built pages), "always" or
    "never"; defaults to INFORMANT_BROWSER or "auto".
    llm_strategy: "auto", "single" (one LLM call) or "staged" (notes per part, then
    a merge call); defaults to INFORMANT_LLM_STRATEGY or "auto".
    use_cache: reuse locally cached pages and LLM answers (see cache.py).
    refresh: re-download pages even if cached (unchanged pages still reuse LLM answers).
    """
    depth = (registry or os.environ.get("INFORMANT_REGISTRY") or "full").strip().lower()
    if depth not in REGISTRY_DEPTHS:
        raise ConfigError(f"registry depth must be full or basic, not {depth!r}.")
    caching = use_cache and cache_enabled()

    own_ch = ch_client is None
    ch = ch_client or CompaniesHouseClient(os.environ.get("COMPANIES_HOUSE_API_KEY", ""))
    try:
        number, matched_by, alternatives = resolve_company(ch, query, progress)
        emit(progress, "companies_house", "started", "Registry data, officers and filings")
        ch_data = fetch_company(ch, number, filings_limit=filings)
        emit(progress, "companies_house", "done", ch_data.identity.name,
             officers=len(ch_data.people.officers), filings=len(ch_data.filings.recent))

        profile = CompanyProfile(
            query=query,
            identity=ch_data.identity,
            classification=ch_data.classification,
            locations=ch_data.locations,
            people=ch_data.people,
            filings=ch_data.filings,
        )
        if depth == "full" and ch_data.raw is not None:
            add_registry_details(ch, profile, ch_data.raw, progress=progress,
                                 accounts_cache=AccountsCache() if caching else None)
        else:
            emit(progress, REGISTRY_STAGE, "skipped", "Basic registry data only")
        add_risk_flags(profile, ch_data.raw)
    finally:
        if own_ch:
            ch.close()

    if web:
        own_crawler = crawler is None
        crawler = crawler or Crawler(
            browser=browser, cache=PageCache(refresh=refresh) if caching else None)
        try:
            research_website(profile, crawler=crawler, user_url=website,
                             max_pages=max_pages, use_llm=llm, llm_strategy=llm_strategy,
                             llm_cache=LlmCache() if caching else None,
                             progress=progress)
        finally:
            if own_crawler:
                crawler.close()
    else:
        for stage in ("website_discovery", "crawl", "extract", "llm"):
            emit(progress, stage, "skipped", "Website research disabled")

    return LookupResult(profile=profile, matched_by=matched_by, alternatives=alternatives)
