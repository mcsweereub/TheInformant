"""UK public-sector contracts awarded to the company, from Contracts Finder's official
search API (free, no key; covers awards published since 2015 by central government, the
NHS, councils and other public bodies).

The keyword search matches supplier names among other text, so each award is kept only
if one of its named suppliers is the company (same name without legal suffixes).
Framework awards name many suppliers; their value is the whole framework's, so only
awards won alone count towards `sole_supplier_value`.
"""

from __future__ import annotations

import html
import json
import re
from collections import Counter
from datetime import date, datetime

import httpx

from ..schema import PublicContract, PublicContracts, Source
from .fetchutil import api_client, cached_post_json
from .website.extract import legal_key

SEARCH_URL = "https://www.contractsfinder.service.gov.uk/api/rest/2/search_notices/json"
NOTICE_URL = "https://www.contractsfinder.service.gov.uk/Notice/{id}"
PAGE_SIZE = 1000  # the API's largest page
MAX_ITEMS = 100
_TRAILER = re.compile(r"\s*(\(.*?\)|\s-\s.*|t/a .*|trading as .*)$", re.I)


def _date(value: str | None) -> date | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date() if value else None
    except ValueError:
        return None


def suppliers_of(field: str | None) -> list[str]:
    """'Softcat plc,Phoenix Software Ltd' -> both names, unescaped."""
    return [s.strip() for s in html.unescape(field or "").split(",") if s.strip()]


def is_supplier(name: str, wanted: str) -> bool:
    """True if a supplier name as written on a notice is the company: 'SOFTCAT LTD - FCA',
    'Softcat Ltd (Head Office)' and 'SoftCat PLC' all match 'softcat'."""
    return bool(wanted) and legal_key(_TRAILER.sub("", name)) == wanted


def search_term(company_name: str) -> str:
    """The registered name without its legal suffix: 'SOFTCAT PLC' -> 'Softcat'."""
    return re.sub(r"\b(limited|ltd|plc|llp|l\.l\.p\.|p\.l\.c\.)\.?$", "", company_name.strip(),
                  flags=re.I).strip().title()


def fetch_contracts(company_name: str, *, cache=None,
                    http: httpx.Client | None = None) -> PublicContracts:
    term = search_term(company_name)
    section = PublicContracts(searched_for=term)
    wanted = legal_key(company_name)
    if len(wanted) < 3:
        section.notes.append("name too short to search contracts reliably")
        return section
    own = http is None
    http = http or api_client(timeout=60)
    # Quoted, or each word is searched for separately ("software" alone matches 20,000+).
    body = {"searchCriteria": {"keyword": f'"{term}"', "statuses": ["Awarded"]}, "size": PAGE_SIZE}
    try:
        data = json.loads(cached_post_json(http, cache, SEARCH_URL, body))
    except (httpx.HTTPError, ValueError) as e:
        section.notes.append(f"Contracts Finder search failed: {type(e).__name__}")
        return section
    finally:
        if own:
            http.close()

    items: list[PublicContract] = []
    for notice in data.get("noticeList") or []:
        it = notice.get("item") or {}
        names = suppliers_of(it.get("awardedSupplier"))
        if not any(is_supplier(n, wanted) for n in names):
            continue
        url = NOTICE_URL.format(id=it["id"]) if it.get("id") else None
        items.append(PublicContract(
            title=html.unescape(it.get("title") or "").strip() or "(untitled)",
            buyer=html.unescape(it.get("organisationName") or "").strip() or None,
            value=it.get("awardedValue") or None,
            awarded_on=_date(it.get("awardedDate")), start=_date(it.get("start")),
            end=_date(it.get("end")), category=it.get("cpvDescription") or None,
            region=it.get("regionText") or None, suppliers=len(set(n.lower() for n in names)) or 1,
            url=url, sources=[Source(name="contracts_finder", url=url)]))

    items.sort(key=lambda c: c.awarded_on or date.min, reverse=True)
    section.found = len(items)
    sole = [c.value for c in items if c.suppliers == 1 and c.value]
    section.sole_supplier_value = round(sum(sole), 2) if sole else None
    section.top_buyers = dict(Counter(c.buyer for c in items if c.buyer).most_common(10))
    section.items = items[:MAX_ITEMS]
    section.sources = [Source(name="contracts_finder", url="https://www.contractsfinder.service.gov.uk")]
    hits = data.get("hitCount") or 0
    if hits > PAGE_SIZE:
        section.notes.append(f"the search matched {hits} notices; only the first {PAGE_SIZE} were checked")
    if len(items) > MAX_ITEMS:
        section.notes.append(f"{len(items)} awards found; the newest {MAX_ITEMS} are listed")
    return section


def contracts_material(section: PublicContracts):
    """The awards as LLM material: who buys from the company and what for."""
    from .website.parse import Page

    if not section.items:
        return None
    lines = []
    for c in section.items[:40]:
        when = c.awarded_on.isoformat() if c.awarded_on else "undated"
        shared = f" (shared with {c.suppliers - 1} other suppliers)" if c.suppliers > 1 else ""
        value = f", £{c.value:,.0f}" if c.value else ""
        lines.append(f"{when}: {c.buyer or 'a public body'} awarded \"{c.title}\"{value}{shared}")
    return Page(url="https://www.contractsfinder.service.gov.uk", kind="public_contracts",
                status_code=200, title="UK public-sector contracts awarded to the company "
                                       "(Contracts Finder)", text="\n".join(lines))
