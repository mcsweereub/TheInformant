"""Find a company's official website.

Order of preference:
  1. URL supplied by the user
  2. Wikidata: entity with this Companies House number (P2622) -> official website (P856)
  3. Guess domains from the company name, then verify ownership on the page
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from .fetch import Crawler
from .extract import core_name, verify_ownership

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"


@dataclass
class Discovery:
    url: str | None
    via: str | None  # user / wikidata / domain_guess
    confidence: str | None
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def wikidata_user_agent() -> str | None:
    """Wikimedia's robot policy requires contact details in the User-Agent
    (https://w.wiki/4wJS), and blocks requests without them. Only query
    Wikidata if the user has opted in by setting INFORMANT_CONTACT."""
    contact = os.environ.get("INFORMANT_CONTACT", "").strip()
    return f"informant/0.1 ({contact}) python-httpx" if contact else None


def wikidata_website(company_number: str, client: httpx.Client | None = None) -> str | None:
    ua = wikidata_user_agent()
    if client is None and ua is None:
        return None
    variants = {company_number, company_number.lstrip("0")}
    values = " ".join(f'"{v}"' for v in variants)
    query = (
        "SELECT ?site WHERE { VALUES ?n { %s } ?item wdt:P2622 ?n . ?item wdt:P856 ?site . } LIMIT 5"
        % values
    )
    http = client or httpx.Client(timeout=15, headers={"User-Agent": ua})
    try:
        resp = http.get(WIKIDATA_SPARQL, params={"query": query, "format": "json"},
                        headers={"Accept": "application/sparql-results+json"})
        resp.raise_for_status()
        rows = resp.json().get("results", {}).get("bindings", [])
    except (httpx.HTTPError, ValueError):
        return None
    finally:
        if client is None:
            http.close()
    sites = [r["site"]["value"] for r in rows if "site" in r]
    # Prefer a .uk domain for UK companies when several are listed.
    sites.sort(key=lambda s: (".uk" not in s, len(s)))
    return sites[0] if sites else None


_LEGAL = ("limited", "ltd", "plc", "llp", "lp")


def candidate_domains(company_name: str, limit: int = 8) -> list[str]:
    core = core_name(company_name).split()
    full = [w for w in "".join(c if c.isalnum() else " " for c in company_name.lower()).split()
            if w not in _LEGAL and w != "the"]
    stems: list[str] = []
    for words in (core, full):
        if not words:
            continue
        for stem in ("".join(words), "-".join(words)):
            if stem and stem not in stems:
                stems.append(stem)
    domains = [f"{s}{tld}" for s in stems for tld in (".co.uk", ".com", ".uk")]
    return list(dict.fromkeys(domains))[:limit]


def first_word_domains(company_name: str) -> list[str]:
    """'MONZO BANK LIMITED' -> monzo.com etc. Only for multi-word names; these
    are riskier guesses, so callers should demand strong verification."""
    words = core_name(company_name).split()
    if len(words) < 2 or len(words[0]) < 4:
        return []
    return [f"{words[0]}{tld}" for tld in (".com", ".co.uk")]


def discover_website(
    crawler: Crawler,
    *,
    company_name: str,
    company_number: str | None,
    postcode: str | None,
    user_url: str | None = None,
    search=None,
) -> Discovery:
    """`search` (a web_search.WebSearch) is the last resort, used only when every free
    method has failed."""
    if user_url:
        return Discovery(url=user_url, via="user", confidence="high",
                         reasons=["supplied by user"])

    notes: list[str] = []
    if company_number and wikidata_user_agent():
        site = wikidata_website(company_number)
        if site:
            return Discovery(url=site, via="wikidata", confidence="high",
                             reasons=[f"Wikidata lists this as the official website for company {company_number}"])
        notes.append("no Wikidata entry with an official website for this company number")

    best: Discovery | None = None
    for domain in candidate_domains(company_name):
        page = crawler.probe(domain)
        if page is None or page.status_code != 200 or not page.text:
            continue
        confidence, reasons = verify_ownership(
            [page], company_number=company_number, company_name=company_name, postcode=postcode
        )
        if confidence == "high":
            return Discovery(url=page.url, via="domain_guess", confidence="high",
                             reasons=reasons, notes=notes)
        if confidence == "medium" and best is None:
            best = Discovery(url=page.url, via="domain_guess", confidence="medium",
                             reasons=reasons, notes=notes)
        elif confidence == "low":
            notes.append(f"{page.url} exists but doesn't look like this company; ignored")
    if best:
        return best

    # Last resort: first word only, accepted only on hard evidence (number/postcode).
    for domain in first_word_domains(company_name):
        page = crawler.probe(domain)
        if page is None or page.status_code != 200 or not page.text:
            continue
        confidence, reasons = verify_ownership(
            [page], company_number=company_number, company_name=company_name, postcode=postcode
        )
        if confidence == "high":
            return Discovery(url=page.url, via="domain_guess", confidence="high",
                             reasons=reasons, notes=notes)
        notes.append(f"{page.url} exists but its homepage doesn't show the company number; ignored")

    if search is not None:
        found = _discover_by_search(crawler, search, company_name, company_number, postcode, notes)
        if found is not None:
            return found
    notes.append("could not find the company website; pass --website to set it")
    return Discovery(url=None, via=None, confidence=None, notes=notes)


# Sites that are about companies, not run by them: never taken as the official website.
_DIRECTORIES = (
    "company-information.service.gov.uk", "gov.uk", "wikipedia.org", "wikidata.org",
    "opencorporates.com", "endole.co.uk", "companycheck.co.uk", "bizdb.co.uk", "dnb.com",
    "bloomberg.com", "crunchbase.com", "zoominfo.com", "glassdoor", "indeed.", "trustpilot.com",
    "yell.com", "companieslist.co.uk", "find-and-update", "linkedin.com", "facebook.com",
    "twitter.com", "x.com", "instagram.com", "youtube.com", "annualreports.com",
)


def _discover_by_search(crawler, search, company_name: str, company_number: str | None,
                        postcode: str | None, notes: list[str]) -> Discovery | None:
    """Search for the company and test the sites that come back. A site is accepted
    on the same evidence as a guessed domain: its homepage shows the company number
    or postcode (high), or names the company in its title (medium)."""
    core = core_name(company_name)
    query = f'"{core}" {company_number}' if company_number else f'"{core}" official website'
    results = search.search(query, max_results=8)
    hosts: list[str] = []
    for r in results:
        host = urlparse(r.url).netloc.lower()
        if host and host not in hosts and not any(d in host for d in _DIRECTORIES):
            hosts.append(host)
    best: Discovery | None = None
    for host in hosts[:4]:
        page = crawler.probe(f"https://{host}/")
        if page is None or page.status_code != 200 or not page.text:
            continue
        confidence, reasons = verify_ownership(
            [page], company_number=company_number, company_name=company_name, postcode=postcode)
        reasons = reasons + ["found by web search"]
        if confidence == "high":
            return Discovery(url=page.url, via="search", confidence="high", reasons=reasons, notes=notes)
        if confidence == "medium" and best is None:
            best = Discovery(url=page.url, via="search", confidence="medium", reasons=reasons, notes=notes)
    if best is None and results:
        notes.append("web search found no site that could be confirmed as the company's")
    return best
