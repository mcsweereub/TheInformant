"""Website stage: discover -> crawl -> extract -> merge into a CompanyProfile."""

from __future__ import annotations

import re

from ...cache import LlmCache
from ...llm import ExtractionResult, current_model, extract_with_llm
from ...progress import ProgressFn, emit
from ...schema import (
    CompanyProfile,
    Location,
    Product,
    Source,
    StructuredData,
    WebPage,
    Website,
)
from .discovery import discover_website
from .extract import (
    detect_tech,
    extract_contacts,
    extract_social_links,
    merge_structured_contacts,
    verify_ownership,
)
from .fetch import Crawler
from .parse import CrawlResult, Page
from .structured import extract_structured

_RANK = {"low": 0, "medium": 1, "high": 2}
_POSTCODE = re.compile(r"[A-Z]{1,2}[0-9][0-9A-Z]? ?[0-9][A-Z]{2}")  # UK postcode


def _page_meta_description(result: CrawlResult) -> str | None:
    from bs4 import BeautifulSoup

    home = next((p for p in result.ok_pages if p.kind == "home"), None)
    if not home:
        return None
    soup = BeautifulSoup(home.html, "html.parser")
    tag = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", attrs={"property": "og:description"}
    )
    return tag.get("content", "").strip() or None if tag else None


def research_website(
    profile: CompanyProfile,
    *,
    crawler: Crawler,
    user_url: str | None = None,
    max_pages: int = 8,
    use_llm: bool = True,
    llm_strategy: str | None = None,
    llm_cache: LlmCache | None = None,
    progress: ProgressFn | None = None,
) -> CompanyProfile:
    """Fill the website-derived sections of `profile` in place and return it."""
    ident = profile.identity
    postcode = next(
        (l.postal_code for l in profile.locations if l.kind == "registered_office"), None
    )

    emit(progress, "website_discovery", "started", "Finding the company website")
    found = discover_website(
        crawler,
        company_name=ident.name,
        company_number=ident.company_number,
        postcode=postcode,
        user_url=user_url,
    )
    site = Website(
        url=found.url,
        discovered_via=found.via,
        confidence=found.confidence,
        verification=list(found.reasons),
        notes=list(found.notes),
    )
    profile.website = site
    if not found.url:
        emit(progress, "website_discovery", "failed", "Website not found")
        for stage in ("crawl", "extract", "llm"):
            emit(progress, stage, "skipped", "No website")
        return profile
    emit(progress, "website_discovery", "done", found.url,
         url=found.url, via=found.via, confidence=found.confidence)

    emit(progress, "crawl", "started", f"Reading up to {max_pages} pages")
    result = crawler.crawl(found.url, max_pages=max_pages)
    site.notes.extend(result.notes)
    site.url = result.final_url or found.url
    pages = result.ok_pages
    site.pages = [
        WebPage(url=p.url, kind=p.kind, status_code=p.status_code, title=p.title,
                text_chars=len(p.text), rendered=p.rendered,
                fetched_at=p.fetched_at, cached=p.from_cache)
        for p in result.pages
    ]
    if not pages:
        site.notes.append("no readable pages; nothing extracted")
        emit(progress, "crawl", "failed", "No readable pages")
        for stage in ("extract", "llm"):
            emit(progress, stage, "skipped", "No readable pages")
        return profile
    rendered = sum(1 for p in pages if p.rendered)
    emit(progress, "crawl", "done",
         f"{len(pages)} pages read" + (f" ({rendered} with the headless browser)" if rendered else ""),
         pages=len(pages), rendered=rendered,
         cached=sum(1 for p in pages if p.from_cache))
    emit(progress, "extract", "started", "Contacts, social links and tech stack")

    # Provenance uses the real download time, which may be earlier for cached pages.
    fetched = [p.fetched_at for p in pages if p.fetched_at]
    web_source = Source(name="website", url=site.url)
    if fetched:
        web_source.retrieved_at = min(fetched)
    site.sources = [web_source]
    site.title = pages[0].title
    site.meta_description = _page_meta_description(result)

    # Re-check ownership with all pages (the footer on any page may hold the number).
    confidence, reasons = verify_ownership(
        pages, company_number=ident.company_number, company_name=ident.name, postcode=postcode
    )
    if found.via != "user" and _RANK[confidence] > _RANK.get(site.confidence or "low", 0):
        site.confidence = confidence
    for r in reasons:
        if r not in site.verification:
            site.verification.append(r)
    if site.confidence == "low":
        site.notes.append("low confidence this site belongs to the company: treat with care")

    site.emails, site.phones = extract_contacts(pages)
    site.social_links = extract_social_links(pages)
    profile.tech_stack = detect_tech(result)
    ident.website = site.url

    # Facts the site declares in machine-readable form: exact, and free.
    facts = extract_structured(pages)
    site.structured = StructuredData(
        name=facts.name, legal_name=facts.legal_name, description=facts.description,
        logo_url=facts.logo_url, founding_date=facts.founding_date, employees=facts.employees,
        vat_id=facts.vat_id, parent_organisation=facts.parent_organisation,
        schema_types=facts.schema_types)
    site.emails, site.phones, site.social_links = merge_structured_contacts(
        site.emails, site.phones, site.social_links,
        raw_emails=facts.emails, raw_phones=facts.telephones, same_as=facts.same_as)
    for a in facts.addresses:
        if _already_listed(profile.locations, a.get("postal_code"), a.get("street")):
            continue
        profile.locations.append(Location(
            kind="office", address_lines=[a["street"]] if a.get("street") else [],
            locality=a.get("locality"), region=a.get("region"),
            postal_code=a.get("postal_code"), country=a.get("country"), sources=[web_source]))
    emit(progress, "extract", "done", None, tech=len(profile.tech_stack),
         social=len(site.social_links), structured=not facts.is_empty())

    if use_llm:
        backend, model = current_model()
        emit(progress, "llm", "started", f"Extracting with {backend}/{model}",
             backend=backend, model=model)
        material = list(pages)
        if facts.as_text():  # give the model the declared facts first: short and reliable
            material.insert(0, Page(url=site.url, kind="structured", status_code=200,
                                    title="Facts this site declares about itself (schema.org data)",
                                    text=facts.as_text()))
        llm = extract_with_llm(ident.name, material, strategy=llm_strategy, progress=progress,
                               cache=llm_cache)
        site.notes.extend(llm.notes)
        _merge_llm(profile, llm, [p.url for p in pages])
        if llm.data is None:
            emit(progress, "llm", "failed", llm.notes[-1] if llm.notes else "No result",
                 strategy=llm.strategy)
        else:
            emit(progress, "llm", "done", None, model=llm.model, strategy=llm.strategy,
                 products=len(profile.products))
    else:
        emit(progress, "llm", "skipped", "LLM disabled")

    # No LLM summary (disabled, or it failed)? The site's own description is the next best.
    if not ident.description:
        ident.description = facts.description or site.meta_description
    return profile


def _already_listed(locations: list[Location], postal_code: str | None, street: str | None) -> bool:
    """True if a location with the same postcode (or the same street line) is already there."""
    def norm(s: str | None) -> str:
        return "".join((s or "").lower().split())

    for loc in locations:
        if postal_code and norm(loc.postal_code) == norm(postal_code):
            return True
        if street and any(norm(street) and norm(street) in norm(line) for line in loc.address_lines):
            return True
    return False


def _source_for(claimed: str, page_urls: list[str], site_url: str | None) -> list[Source]:
    """The single page an item came from. The model names it; we only accept a
    page we actually crawled, otherwise we fall back to the website itself."""
    def norm(u: str) -> str:
        return u.strip().rstrip("/").lower()

    by_norm = {norm(u): u for u in page_urls}
    url = by_norm.get(norm(claimed)) if claimed else None
    return [Source(name="llm_extraction", url=url or site_url)]


def _merge_llm(profile: CompanyProfile, llm: ExtractionResult, page_urls: list[str]) -> None:
    if llm.data is None:
        return
    d = llm.data
    site_url = profile.website.url

    profile.website.llm_model = llm.model
    profile.identity.description = d.summary
    profile.website.business_model = d.business_model
    profile.website.target_markets = d.target_markets

    cls = profile.classification
    cls.sectors = list(dict.fromkeys(cls.sectors + d.sectors))
    cls.tags = sorted(set(cls.tags) | set(d.tags))
    cls.sources.append(Source(name="llm_extraction", url=site_url))

    profile.products = [
        Product(name=p.name, description=p.description, category=p.category,
                sources=_source_for(p.source_page, page_urls, site_url))
        for p in d.products
    ]
    for o in d.offices:
        postcode = _POSTCODE.search(f"{o.address} {o.label}".upper())
        if (o.address or postcode) and _already_listed(
                profile.locations, postcode.group(0) if postcode else None, o.address or None):
            continue  # the same address already came from the registry or structured data
        profile.locations.append(
            Location(kind="office", address_lines=[o.label, o.address] if o.address else [o.label],
                     locality=o.locality or None, country=o.country or None,
                     sources=_source_for(o.source_page, page_urls, site_url))
        )
