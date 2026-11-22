"""Research steps for the other public records: LEI (GLEIF), public contracts, job
boards, site feeds, domain signals, political donations and property. Each fills one
profile section and returns LLM material (or None); a failing source leaves a note."""

from __future__ import annotations

from .group import group_companies, overseas_group_companies
from .progress import ProgressFn, emit
from .schema import CompanyProfile
from .sources import land_registry
from .sources.contracts import contracts_material, fetch_contracts
from .sources.domain import fetch_domain_signals
from .sources.donations import fetch_donations, fetch_group_donations
from .sources.gleif import fetch_lei
from .sources.jobs import fetch_jobs, find_boards, jobs_material
from .sources.land_registry import (
    OVERSEAS,
    PropertyStore,
    fetch_group_property,
    fetch_overseas_group_property,
    fetch_property,
    is_overseas_number,
    keep_current,
    property_material,
)
from .sources.news import search_names
from .sources.website.feeds import feeds_material, shopify_catalogue, wordpress_posts
from .sources.website.fetch import Crawler
from .sources.website.parse import CrawlResult, Page


def _known_website(profile: CompanyProfile) -> str | None:
    """The company's site: the crawled one, else the one Wikidata lists."""
    return profile.identity.website or next(iter(profile.wikipedia.official_websites), None)


def research_lei(profile: CompanyProfile, *, cache=None, progress: ProgressFn | None = None) -> None:
    """Fill profile.lei from GLEIF (by company number, else exact UK legal name)."""
    ident = profile.identity
    emit(progress, "lei", "started", "Looking up the Legal Entity Identifier (GLEIF)")
    profile.lei = fetch_lei(ident.company_number or "", ident.name, cache=cache).section
    lei = profile.lei
    if lei.lei:
        emit(progress, "lei", "done", lei.lei, lei=lei.lei, subsidiaries=lei.subsidiaries_total)
    else:
        emit(progress, "lei", "skipped", lei.notes[-1] if lei.notes else "No LEI")


def research_contracts(profile: CompanyProfile, *, cache=None,
                       progress: ProgressFn | None = None) -> Page | None:
    """Fill profile.public_contracts; returns the awards as LLM material (or None)."""
    emit(progress, "contracts", "started", "Searching UK public-sector contracts")
    profile.public_contracts = fetch_contracts(profile.identity.name, cache=cache)
    pc = profile.public_contracts
    emit(progress, "contracts", "done", f"{pc.found} contract award(s)", found=pc.found)
    return contracts_material(pc)


def research_jobs(profile: CompanyProfile, result: CrawlResult | None, *, cache=None,
                  progress: ProgressFn | None = None) -> list[Page]:
    """Fill profile.hiring from the job board the site links to (or one found by name);
    returns the roles as LLM material."""
    ident = profile.identity
    emit(progress, "jobs", "started", "Looking for a public job board")
    pages = result.ok_pages if result is not None else []
    boards = find_boards([p.html for p in pages], [href for p in pages for href, _ in p.links])
    names = search_names(ident.name, _known_website(profile))
    profile.hiring = fetch_jobs(ident.name, boards=boards, names=names, cache=cache)
    h = profile.hiring
    if h.provider:
        emit(progress, "jobs", "done", f"{h.total} open role(s) on {h.provider}",
             provider=h.provider, roles=h.total)
    else:
        emit(progress, "jobs", "skipped", h.notes[-1] if h.notes else "No job board")
    material = jobs_material(h)
    return [material] if material is not None else []


def research_site_feeds(profile: CompanyProfile, crawler: Crawler, *,
                        progress: ProgressFn | None = None) -> list[Page]:
    """WordPress posts and the Shopify catalogue, when the site runs on them."""
    site = profile.website
    if not site.url:
        return []
    wp, shop = "WordPress" in profile.tech_stack, "Shopify" in profile.tech_stack
    if not (wp or shop):
        emit(progress, "feeds", "skipped", "Not a WordPress or Shopify site")
        return []
    emit(progress, "feeds", "started", "Reading the site's own feeds")
    if wp:
        site.posts = wordpress_posts(crawler, site.url)
    if shop:
        site.shop = shopify_catalogue(crawler, site.url)
        if site.shop is None:
            site.notes.append("Shopify product feed not available")
    emit(progress, "feeds", "done", f"{len(site.posts)} post(s)"
         + (f", {site.shop.products_listed} products" if site.shop else ""),
         posts=len(site.posts), products=site.shop.products_listed if site.shop else 0)
    return feeds_material(site.posts, site.shop, site.url)


def research_domain(profile: CompanyProfile, *, cache=None,
                    progress: ProgressFn | None = None) -> None:
    """Fill profile.domain: DNS, certificate logs and the Wayback Machine."""
    website = _known_website(profile)
    if not website:
        emit(progress, "domain", "skipped", "No website known")
        return
    emit(progress, "domain", "started", "Reading DNS, certificate logs and the web archive")
    profile.domain = fetch_domain_signals(website, incorporated_on=profile.identity.incorporated_on,
                                          cache=cache)
    d = profile.domain
    emit(progress, "domain", "done", d.email_provider or d.name, email_provider=d.email_provider,
         subdomains=len(d.subdomains))


def research_donations(profile: CompanyProfile, *, cache=None,
                       progress: ProgressFn | None = None) -> None:
    """Fill profile.political_donations from the Electoral Commission's register, searching
    the current and previous names and matching on the company number; then the same for
    the other UK companies in its group (parents and LEI subsidiaries)."""
    ident = profile.identity
    emit(progress, "donations", "started", "Searching the register of political donations")
    names = [ident.name] + [p.name for p in ident.previous_names]
    earlier = profile.political_donations  # the accounts statement may already be there
    pd = fetch_donations(ident.company_number or "", names, cache=cache)
    pd.accounts_statement = earlier.accounts_statement
    pd.accounts_statement_url = earlier.accounts_statement_url
    profile.political_donations = pd

    group = group_companies(profile)
    if group:
        emit(progress, "donations", "progress", f"Checking {len(group)} other group companies")
        pd.group, notes = fetch_group_donations(group, cache=cache)
        pd.group_searched = [f"{name} ({number}, {relation})" for number, name, relation in group]
        pd.notes.extend(notes)
    in_group = sum(g.found for g in pd.group)
    emit(progress, "donations", "done", f"{pd.found} donation(s)"
         + (f", {in_group} by group companies" if in_group else ""),
         found=pd.found, total_value=pd.total_value, group_found=in_group)


def research_property(profile: CompanyProfile, *, store: PropertyStore | None = None,
                      overseas_store: PropertyStore | None = None,
                      progress: ProgressFn | None = None):
    """Fill profile.property from the local copies of HM Land Registry's UK and overseas
    companies datasets (applying newer monthly change files first, at most once a day),
    with totals for the rest of the group: UK group companies by number, overseas ones by
    exact name and country. Returns LLM material (or None)."""
    store = store or PropertyStore()
    overseas_store = overseas_store or PropertyStore(dataset=OVERSEAS)
    ident = profile.identity
    # an overseas company registered here (FC/OE number) is in the overseas dataset
    is_overseas = is_overseas_number(ident.company_number)
    own_store = overseas_store if is_overseas else store
    if not store.exists() and not overseas_store.exists():
        emit(progress, "property", "skipped", "No local Land Registry data "
                                               "(run `informant property --update`)")
        profile.property = fetch_property(None, store=store)  # carries the how-to note
        return None
    emit(progress, "property", "started", "Reading Land Registry ownership data")
    notify = lambda m: emit(progress, "property", "progress", m)  # noqa: E731
    notes = [n for s in (store, overseas_store) if s.exists() for n in [keep_current(s, notify)] if n]
    names = [ident.name] + [p.name for p in ident.previous_names]
    holdings = fetch_property(ident.company_number, names, store=own_store)
    holdings.notes.extend(notes)
    group = group_companies(profile) if store.exists() else []
    if group:
        holdings.group = fetch_group_property(group, store=store)
        holdings.group_searched = [f"{name} ({number}, {relation})" for number, name, relation in group]
    abroad = overseas_group_companies(profile) if overseas_store.exists() else []
    if abroad:
        found, found_notes = fetch_overseas_group_property(abroad, overseas_store)
        holdings.group.extend(found)
        holdings.notes.extend(found_notes)
        holdings.group_searched += [f"{name} ({country or 'overseas'}, {relation})"
                                    for _, name, country, relation in abroad]
    if overseas_store.exists() and (abroad or is_overseas):
        meta = overseas_store.meta()
        holdings.overseas_dataset = meta.get("dataset") or None
        holdings.overseas_data_as_of = land_registry.date_or_none(meta.get("published"))
        if is_overseas and store.exists():
            uk_meta = store.meta()
            holdings.dataset = uk_meta.get("dataset") or None
            holdings.data_as_of = land_registry.date_or_none(uk_meta.get("published"))
        if not any(s.url == land_registry.dataset_page(OVERSEAS) for s in holdings.sources):
            holdings.sources.append(land_registry.source_for(overseas_store, meta))
    profile.property = holdings
    in_group = sum(g.titles for g in holdings.group)
    emit(progress, "property", "done", f"{holdings.found} title(s)"
         + (f", {in_group} owned by group companies" if in_group else ""),
         found=holdings.found, group_titles=in_group)
    return property_material(holdings)


