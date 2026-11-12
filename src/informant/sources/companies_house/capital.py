"""Share capital filings and charges.

Share capital: every allotment of new shares must be filed (form SH01), so the
filing history shows when a company issued shares, which usually means a funding
round or an employee share scheme. The filing metadata gives the date and the
total nominal share capital afterwards (not the price paid: that is only in the
filed document itself).

Charges: security given to lenders (mortgages, debentures). They show who
finances the company and whether that borrowing is still outstanding.
"""

from __future__ import annotations

from ...schema import Capital, CapitalEvent, Charge, Charges
from .client import CompaniesHouseClient
from .mapping import filing_document_url, source

MAX_CHARGES = 50

# filing description keyword -> kind, checked in order
_KINDS: tuple[tuple[str, str], ...] = (
    ("allotment", "allotment"),
    ("cancellation", "cancellation"),
    ("purchase-own-shares", "buyback"),
    ("redemption", "buyback"),
    ("reduction", "reduction"),
    ("sub-division", "subdivision"),
    ("consolidation", "subdivision"),
)


def _capital_kind(item: dict) -> str:
    desc = (item.get("description") or "").lower()
    for keyword, kind in _KINDS:
        if keyword in desc:
            return kind
    form = (item.get("type") or "").upper()
    if form.endswith("SH01") or form.startswith("88(2)"):  # 88(2): pre-2009 allotment form
        return "allotment"
    if form in ("SH19", "SH20"):
        return "reduction"
    if form == "169":  # pre-2009 return of purchase of own shares
        return "buyback"
    if form == "122":  # pre-2009 notice of cancellation / consolidation
        return "cancellation"
    return "other"


def map_capital_event(number: str, item: dict) -> CapitalEvent:
    values = item.get("description_values") or {}
    capital = values.get("capital") or []
    first = capital[0] if capital else {}
    try:
        figure = float(str(first.get("figure", "")).replace(",", "")) if first.get("figure") else None
    except ValueError:
        figure = None
    description = values.get("description") or (item.get("description") or "").replace("-", " ")
    return CapitalEvent(
        kind=_capital_kind(item),
        form=item.get("type"),
        event_date=values.get("date") or item.get("date"),
        filed_on=item.get("date"),
        description=description or None,
        share_capital=figure,
        currency=first.get("currency"),
        document_url=filing_document_url(number, item),
    )


def fetch_capital(client: CompaniesHouseClient, number: str, limit: int = 100) -> Capital:
    items = client.filing_history(number, limit=limit, category="capital")
    events = []
    for item in items:
        try:
            events.append(map_capital_event(number, item))
        except ValueError:  # an unparseable date in a legacy filing
            continue
    return Capital(events=events, sources=[source(number, "/filing-history?category=capital")])


def map_charges(number: str, data: dict) -> Charges:
    items = data.get("items") or []
    charges = []
    for c in items[:MAX_CHARGES]:
        try:
            charges.append(Charge(
                charge_number=c.get("charge_number"),
                status=c.get("status"),
                classification=(c.get("classification") or {}).get("description"),
                created_on=c.get("created_on"),
                delivered_on=c.get("delivered_on"),
                satisfied_on=c.get("satisfied_on"),
                lenders=[p["name"] for p in c.get("persons_entitled") or [] if p.get("name")],
                particulars=((c.get("particulars") or {}).get("description") or "")[:400] or None,
                secured=((c.get("secured_details") or {}).get("description") or "")[:300] or None,
            ))
        except ValueError:
            continue
    total = data.get("total_count") or len(items)
    satisfied = data.get("satisfied_count") or 0
    part = data.get("part_satisfied_count") or 0
    return Charges(
        total=total,
        satisfied=satisfied,
        part_satisfied=part,
        outstanding=max(total - satisfied - part, 0),
        items=charges,
        sources=[source(number, "/charges")] if total else [],
    )


def fetch_charges(client: CompaniesHouseClient, number: str) -> Charges:
    return map_charges(number, client.charges(number))
