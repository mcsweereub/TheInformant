"""One company's titles (and its group's) from the local copies."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Iterable
from datetime import date, datetime

from ...schema import CoOwner, GroupProperty, PropertyHoldings, PropertyTitle, Source, utcnow
from ..website.extract import legal_key
from .common import (
    DATASET_PAGE,
    MAX_ITEMS,
    OVERSEAS,
    UK,
    dataset_page,
    is_overseas_number,
    number_key,
)
from .countries import same_country
from .records import owner_keys
from .store import PropertyStore

# --- one company ----------------------------------------------------------------------------


def _owners(row: sqlite3.Row) -> list[tuple[str, str, str, str, str]]:
    """(name, number, category, address, country) for each proprietor on the title."""
    out = []
    for entry in json.loads(row["owners"] or "[]"):
        name, number, category, address, *rest = entry  # older copies have no country
        out.append((name, number, category, address, rest[0] if rest else ""))
    return out


def _title(row: sqlite3.Row, own_keys: set[str], matched_by: str, dataset: str = UK) -> PropertyTitle:
    mine, others = None, []
    for name, number, category, address, country in _owners(row):
        if mine is None and own_keys & set(owner_keys(name, number, country)):
            mine = address
        else:
            uk_number = number if (not country or is_overseas_number(number)) else ""
            others.append(CoOwner(name=name, company_number=uk_number or None,
                                  category=category or None, country=country or None))
    return PropertyTitle(
        title_number=row["title_number"], tenure=row["tenure"] or None, address=row["address"] or None,
        district=row["district"] or None, county=row["county"] or None, region=row["region"] or None,
        postcode=row["postcode"] or None, multiple_addresses=bool(row["multiple"]),
        price_paid=row["price"], owner_since=date.fromisoformat(row["added"]) if row["added"] else None,
        owner_address=mine or None, co_owners=others, matched_by=matched_by, dataset=dataset)


def name_variants(name: str) -> list[str]:
    """Exact-name keys for one name, allowing only for how initials are written: the
    register has both "J C BAMFORD EXCAVATORS" and "J.C. BAMFORD EXCAVATORS"."""
    spaced = legal_key(name.replace(".", " "))
    tokens, joined, run = spaced.split(), [], ""
    for tok in tokens:
        if len(tok) == 1 and tok.isalpha():
            run += tok
            continue
        if run:
            joined.append(run)
            run = ""
        joined.append(tok)
    if run:
        joined.append(run)
    return [k for k in dict.fromkeys([legal_key(name), spaced, " ".join(joined)]) if k]


def _keys(company_number: str | None, names: Iterable[str]) -> tuple[list[str], list[str]]:
    by_number = [f"n:{number_key(company_number)}"] if number_key(company_number) else []
    by_name = list(dict.fromkeys(f"k:{key}" for n in names if n for key in name_variants(n)))
    return by_number, by_name


def fetch_property(company_number: str | None, names: Iterable[str] = (),
                   store: PropertyStore | None = None) -> PropertyHoldings:
    """Titles registered to the company: by company number, plus titles whose owner entry
    has no number but exactly the company's (current or previous) name."""
    store = store or PropertyStore()
    holdings = PropertyHoldings()
    if not store.exists():
        holdings.notes.append("no local copy of the Land Registry dataset: run "
                              "`informant property --update` (downloads ~1.6 GB of data once)")
        return holdings
    ds = store.dataset
    meta = store.meta()
    if ds == OVERSEAS:
        holdings.overseas_dataset = meta.get("dataset") or None
        holdings.overseas_data_as_of = _date_iso(meta.get("published"))
    else:
        holdings.dataset = meta.get("dataset") or None
        holdings.data_as_of = _date_iso(meta.get("published"))
    holdings.sources = [source_for(store, meta)]
    by_number, by_name = _keys(company_number, names)
    own = set(by_number + by_name)
    rows = store.titles_for(by_number + by_name)
    titles = [_title(row, own, "company_number" if key.startswith("n:") else "name", ds)
              for key, row in rows]
    _summarise(holdings, titles)
    holdings.items = sorted(titles, key=lambda t: (t.owner_since or date.min, t.title_number),
                            reverse=True)[:MAX_ITEMS]
    named = sum(1 for t in titles if t.matched_by == "name")
    if named:
        holdings.notes.append(f"{named} title(s) matched on the exact name (no company number "
                              "on the register entry)")
    if len(titles) > MAX_ITEMS:
        holdings.notes.append(f"{len(titles)} titles: the newest {MAX_ITEMS} are listed, "
                              "the totals cover all of them")
    return holdings


def _summarise(target, titles: list[PropertyTitle]) -> None:
    tenure = Counter((t.tenure or "").lower() for t in titles)
    prices = [t.price_paid for t in titles if t.price_paid]
    dates = [t.owner_since for t in titles if t.owner_since]
    if hasattr(target, "found"):
        target.found = len(titles)
        target.priced_titles = len(prices)
        target.first_acquired = min(dates, default=None)
        target.last_acquired = max(dates, default=None)
    else:
        target.titles = len(titles)
    target.freehold = tenure.get("freehold", 0)
    target.leasehold = tenure.get("leasehold", 0)
    target.total_price_paid = round(sum(prices), 2) if prices else None
    target.regions = dict(Counter(t.region for t in titles if t.region).most_common())


def fetch_group_property(companies: list[tuple[str, str, str]],
                         store: PropertyStore | None = None) -> list[GroupProperty]:
    """Totals for other group companies (number, name, relation), matched on number only."""
    store = store or PropertyStore()
    out = []
    for number, name, relation in companies:
        by_number, _ = _keys(number, ())
        own = set(by_number)
        titles = [_title(row, own, "company_number") for _, row in store.titles_for(by_number)]
        if titles:
            g = GroupProperty(name=name, company_number=number, relation=relation)
            _summarise(g, titles)
            out.append(g)
    return out


def fetch_overseas_group_property(companies: list[tuple[str | None, str, str | None, str]],
                                  store: PropertyStore) -> tuple[list[GroupProperty], list[str]]:
    """Totals for overseas group companies (number, name, country, relation) from the
    overseas companies dataset: by FC/OE number when known, else by exact name (allowing
    for how initials are written). A title whose owner of that name was incorporated in a
    different country is not counted. Returns (groups, notes)."""
    out, notes = [], []
    for number, name, country, relation in companies:
        by_number = [f"n:{number_key(number)}"] if is_overseas_number(number) else []
        by_name = [f"k:{k}" for k in name_variants(name)]
        own = set(by_number + by_name)
        titles, other_country = [], set()
        for key, row in store.titles_for(by_number + by_name):
            if key.startswith("k:"):
                theirs = next((c for n, num, _, _, c in _owners(row)
                               if own & set(owner_keys(n, num, c))), "")
                if same_country(country, theirs) is False:
                    other_country.add(theirs)
                    continue
                country = country or theirs or None
            titles.append(_title(row, own, "company_number" if key.startswith("n:") else "name",
                                 OVERSEAS))
        if other_country:
            notes.append(f"a company named {name} incorporated in {', '.join(sorted(other_country))} "
                         f"(not {country}) owns titles: not counted")
        if titles:
            matched = "company_number" if all(t.matched_by == "company_number" for t in titles) else "name"
            g = GroupProperty(name=name, company_number=number if is_overseas_number(number) else None,
                              relation=relation, dataset=OVERSEAS, matched_by=matched,
                              country=country)
            _summarise(g, titles)
            out.append(g)
    return out, notes


def source_for(store: PropertyStore, meta: dict | None = None) -> Source:
    meta = store.meta() if meta is None else meta
    return Source(name="land_registry", url=dataset_page(store.dataset),
                  retrieved_at=_datetime_iso(meta.get("updated_at")) or utcnow())


def _date_iso(text: str | None) -> date | None:
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


date_or_none = _date_iso


def _datetime_iso(text: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(text) if text else None
    except ValueError:
        return None


def property_material(holdings: PropertyHoldings, limit: int = 15):
    """LLM material: where the company owns land or buildings, from the register."""
    from ..website.parse import Page

    overseas = [g for g in holdings.group if g.dataset == OVERSEAS]
    if not holdings.found and not overseas:
        return None
    lines = []
    if holdings.found:
        lines.append(f"The company owns {holdings.found} registered title(s) in England and Wales "
                     f"({holdings.freehold} freehold, {holdings.leasehold} leasehold).")
    for t in holdings.items[:limit]:
        lines.append(f"{t.tenure or 'Title'}: {t.address or t.title_number}"
                     + (f" (owner since {t.owner_since:%Y})" if t.owner_since else ""))
    for g in overseas:
        lines.append(f"Its {g.relation} {g.name}" + (f" ({g.country.title()})" if g.country else "")
                     + f" owns {g.titles} title(s) in England and Wales.")
    return Page(url=DATASET_PAGE, kind="property", status_code=200,
                title="Property owned by the company (HM Land Registry)", text="\n".join(lines))
