"""Website stage: discover -> crawl -> extract -> merge into a CompanyProfile."""

from __future__ import annotations

from .crawler import Crawler, CrawlResult
from .discovery import discover_website
from .extract import detect_tech, extract_contacts, extract_social_links, verify_ownership
from .llm import ExtractionResult, extract_with_llm
from .schema import CompanyProfile, Location, Product, Source, WebPage, Website

_RANK = {"low": 0, "medium": 1, "high": 2}


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
) -> CompanyProfile:
    """Fill the website-derived sections of `profile` in place and return it."""
    ident = profile.identity
    postcode = next(
        (l.postal_code for l in profile.locations if l.kind == "registered_office"), None
    )

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
        return profile

    result = crawler.crawl(found.url, max_pages=max_pages)
    site.notes.extend(result.notes)
    site.url = result.final_url or found.url
    pages = result.ok_pages
    site.pages = [
        WebPage(url=p.url, kind=p.kind, status_code=p.status_code, title=p.title,
                text_chars=len(p.text))
        for p in result.pages
    ]
    if not pages:
        site.notes.append("no readable pages; nothing extracted")
        return profile

    web_source = Source(name="website", url=site.url)
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

    if use_llm:
        llm = extract_with_llm(ident.name, pages)
        site.notes.extend(llm.notes)
        _merge_llm(profile, llm, [p.url for p in pages])
    return profile


def _merge_llm(profile: CompanyProfile, llm: ExtractionResult, page_urls: list[str]) -> None:
    if llm.data is None:
        return
    d = llm.data
    src = [Source(name="llm_extraction", url=u) for u in page_urls]

    profile.website.llm_model = llm.model
    profile.identity.description = d.summary
    profile.website.business_model = d.business_model
    profile.website.target_markets = d.target_markets

    cls = profile.classification
    cls.sectors = list(dict.fromkeys(cls.sectors + d.sectors))
    cls.tags = sorted(set(cls.tags) | set(d.tags))
    cls.sources.append(Source(name="llm_extraction", url=profile.website.url))

    profile.products = [
        Product(name=p.name, description=p.description, category=p.category, sources=src)
        for p in d.products
    ]
    for o in d.offices:
        profile.locations.append(
            Location(kind="office", address_lines=[o.label, o.address] if o.address else [o.label],
                     locality=o.locality or None, country=o.country or None, sources=src)
        )
