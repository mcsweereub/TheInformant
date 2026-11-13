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
from .llm import judge_headlines, make_complete
from .llm.news import SENTIMENT_SCORES
from .progress import ProgressFn, emit
from .schema import CompanyProfile, News, NewsItem, Source
from .sources.companies_house import (
    CompaniesHouseClient,
    SearchHit,
    fetch_company,
    normalise_company_number,
)
from .sources.companies_house.details import STAGE as REGISTRY_STAGE
from .sources.companies_house.details import add_registry_details, add_risk_flags
from .sources.news import DEFAULT_DAYS as DEFAULT_NEWS_DAYS
from .sources.news import fetch_news
from .sources.website.research import summary_sentences
from .sources.wikipedia import fetch_wikipedia
from .sources.website.fetch import Crawler
from .sources.website.research import extract_from_material, research_website

REGISTRY_DEPTHS = ("full", "basic")
_OFF = {"0", "off", "false", "no"}


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
    news: bool | None = None,
    wikipedia: bool | None = None,
    news_days: int | None = None,
    progress: ProgressFn | None = None,
    ch_client: CompaniesHouseClient | None = None,
    crawler: Crawler | None = None,
) -> LookupResult:
    """Build a CompanyProfile for a company name or number (does not save it).

    news: search recent news (default: INFORMANT_NEWS, on); news_days: how far back
    (default: INFORMANT_NEWS_DAYS or 90).
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

    if wikipedia is None:
        wikipedia = os.environ.get("INFORMANT_WIKIPEDIA", "on").strip().lower() not in _OFF
    wiki_material = None
    if wikipedia:
        wiki_material = research_wikipedia(
            profile, cache=PageCache(refresh=refresh) if caching else None, progress=progress)
    else:
        emit(progress, "wikipedia", "skipped", "Wikipedia disabled")

    if web:
        own_crawler = crawler is None
        crawler = crawler or Crawler(
            browser=browser, cache=PageCache(refresh=refresh) if caching else None)
        try:
            research_website(profile, crawler=crawler, user_url=website,
                             max_pages=max_pages, use_llm=llm, llm_strategy=llm_strategy,
                             llm_cache=LlmCache() if caching else None,
                             progress=progress,
                             extra_material=[wiki_material] if wiki_material else None)
        finally:
            if own_crawler:
                crawler.close()
    else:
        for stage in ("website_discovery", "crawl", "extract"):
            emit(progress, stage, "skipped", "Website research disabled")
        if llm and wiki_material is not None:
            # No website, but the Wikipedia article is still worth reading.
            extract_from_material(profile, [wiki_material], llm_strategy=llm_strategy,
                                  llm_cache=LlmCache() if caching else None, progress=progress)
        else:
            emit(progress, "llm", "skipped",
                 "Website research disabled" if llm else "LLM disabled")
        if not profile.identity.description:
            profile.identity.description = summary_sentences(profile.wikipedia.summary)

    if news is None:
        news = os.environ.get("INFORMANT_NEWS", "on").strip().lower() not in _OFF
    if news:
        research_news(profile,
                      days=news_days or int(os.environ.get("INFORMANT_NEWS_DAYS", DEFAULT_NEWS_DAYS)),
                      use_llm=llm,
                      page_cache=PageCache(refresh=refresh) if caching else None,
                      llm_cache=LlmCache() if caching else None,
                      progress=progress)
    else:
        emit(progress, "news", "skipped", "News search disabled")

    return LookupResult(profile=profile, matched_by=matched_by, alternatives=alternatives)


def research_wikipedia(profile: CompanyProfile, *, cache: PageCache | None = None,
                       progress: ProgressFn | None = None):
    """Fill profile.wikipedia and return the article as LLM material (or None).
    Wikidata's industries are added to the tags: they're curated facts."""
    ident = profile.identity
    emit(progress, "wikipedia", "started", "Looking up Wikipedia and Wikidata")
    result = fetch_wikipedia(ident.name, ident.company_number, cache=cache)
    profile.wikipedia = result.section
    industries = [i.lower() for i in result.section.industries if i]
    if industries:
        cls = profile.classification
        cls.tags = sorted(set(cls.tags) | set(industries))
        cls.sources.extend(result.section.sources[:1])
    if result.section.title or result.section.wikidata_id:
        emit(progress, "wikipedia", "done", result.section.title or result.section.wikidata_id,
             matched_by=result.section.matched_by, url=result.section.url)
    else:
        emit(progress, "wikipedia", "skipped",
             result.section.notes[-1] if result.section.notes else "Not found")
    return result.material


def research_news(profile: CompanyProfile, *, days: int = DEFAULT_NEWS_DAYS, limit: int = 20,
                  use_llm: bool = True, page_cache: PageCache | None = None,
                  llm_cache: LlmCache | None = None, progress: ProgressFn | None = None) -> None:
    """Fill profile.news: recent headlines naming the company, each judged by the
    LLM (if enabled) for relevance, tone and topic. Never raises for a provider or
    model failure: those become notes."""
    ident = profile.identity
    emit(progress, "news", "started", "Searching recent news")
    with Crawler(cache=page_cache) as robots:  # only used for its robots.txt check
        found = fetch_news(ident.name, ident.website, days=days, limit=limit,
                           cache=page_cache, allowed=robots.allowed)
    section = News(searched_for=found.names, window_days=days, providers=found.providers,
                   notes=list(found.notes), sources=[Source(name="news")])

    judged = {}
    if use_llm and found.articles:
        emit(progress, "news", "progress", f"Judging {len(found.articles)} headlines",
             headlines=len(found.articles))
        complete, _ = make_complete(cache=llm_cache)
        judged, notes = judge_headlines(ident.name, ident.description,
                                        [a.title for a in found.articles], complete)
        section.notes.extend(notes)

    doubtful = 0
    for i, article in enumerate(found.articles):
        verdict = judged.get(i)
        if verdict is not None and not verdict.about_company:
            doubtful += 1
        section.items.append(NewsItem(
            title=article.title, url=article.url, published_at=article.published_at,
            publisher=article.publisher, summary=article.summary,
            sentiment=SENTIMENT_SCORES[verdict.sentiment] if verdict else None,
            topic=verdict.topic if verdict else None,
            provider=article.provider, relevant=verdict.about_company if verdict else None,
            sources=[Source(name="news", url=article.url)]))
    if doubtful:
        section.notes.append(f"{doubtful} headline(s) marked relevant=false: the model judged "
                             "them not to be about this company (kept, as small models misjudge some)")
    profile.news = section
    relevant = sum(1 for n in section.items if n.relevant is not False)
    emit(progress, "news", "done", f"{relevant} article(s)",
         articles=len(section.items), relevant=relevant, providers=found.providers)
