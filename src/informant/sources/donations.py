"""Political donations made by the company (and its group), from the Electoral Commission's
register of donations (the official search service's JSON API; free, no key, no
robots.txt), plus what the company's own accounts say about political donations.

The register records donations above the reporting threshold to UK political parties,
referendum campaigners and individual politicians (MPs, candidates, councillors). Its
search works on names, so the company's current and previous names are searched, and a
donation is kept only when the donor's Companies House number is the company's (or, for
the few company donors filed without a number, when the donor name is exactly the
company's legal name). Recipients are named, politicians included: they are public
figures, recorded in an official register. Individual donors are never read.

Groups often donate through another company, so the UK parents (from the PSC chain and
the LEI record) and UK subsidiaries with LEIs are searched too, each matched on its own
company number and reported separately in `group`.

Companies that give more than £2,000 in a year must say so in the directors' report;
many also state that they made none. `political_statement` finds those sentences in the
accounts' text (iXBRL filing, annual report or OCR) without an LLM.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, date, datetime, timedelta

import httpx

from ..schema import GroupDonations, PoliticalDonation, PoliticalDonations, Source
from .fetchutil import api_client, cached_get
from .website.extract import legal_key

SEARCH_URL = "https://search.electoralcommission.org.uk/api/search/Donations"
ENTRY_URL = "https://search.electoralcommission.org.uk/English/Donations/{ref}"
PAGE_SIZE = 50  # the API's largest page
MAX_PAGES = 20  # per name searched
MAX_ITEMS = 200
MAX_GROUP_ITEMS = 50  # per group company
MAX_GROUP_COMPANIES = 8
MAX_NAMES = 3  # current name + previous names
# Recipients of type "Regulated Donee" that are organisations, not people.
ORGANISATION_DONEES = {"members association"}
_DATE = re.compile(r"/Date\((-?\d+)\)/")


def ec_date(value: str | None) -> date | None:
    """'/Date(1792623600000)/' -> a date. The register stores UK midnight, which is 23:00
    UTC the day before in summer, so half a day is added before taking the date."""
    m = _DATE.match(value or "")
    if not m:
        return None
    return (datetime.fromtimestamp(int(m.group(1)) / 1000, tz=UTC) + timedelta(hours=12)).date()


def query_for(name: str) -> str:
    """The register's search ANDs words and treats punctuation badly ('J.C.' finds a
    fraction of 'J C'), so: no legal suffix, no punctuation, no 'and'/'&'."""
    words = re.sub(r"[^a-z0-9]+", " ", name.lower()).split()  # 'J.C.' -> 'j c'
    if words[-3:] == ["p", "l", "c"]:
        words = words[:-3]
    elif words and words[-1] in {"limited", "ltd", "plc", "llp", "lp"}:
        words = words[:-1]
    return " ".join(w for w in words if w not in {"and", "the"})


def _compact(name: str) -> str:
    """For comparing donor names: 'J C Bamford Excavators Ltd' and 'J.C. BAMFORD EXCAVATORS
    LIMITED' -> 'jcbamfordexcavators'."""
    return legal_key(name).replace(" ", "")


def _number(value: str | None) -> str:
    return (value or "").strip().upper().lstrip("0")


def is_individual(row: dict) -> bool:
    return (row.get("RegulatedEntityType") == "Regulated Donee"
            and (row.get("RegulatedDoneeType") or "").lower() not in ORGANISATION_DONEES)


def fetch_donations(company_number: str, names: list[str], *, cache=None,
                    http: httpx.Client | None = None) -> PoliticalDonations:
    """Donations by the company under any of `names` (current name first)."""
    names = [n for n in dict.fromkeys(names) if query_for(n)][:MAX_NAMES]
    section = PoliticalDonations(searched_for=[query_for(n) for n in names])
    own_number, legal_names = _number(company_number), {_compact(n) for n in names}
    own = http is None
    http = http or api_client(timeout=60)
    rows: dict[int, tuple[dict, str]] = {}
    other_numbers: dict[int, str] = {}  # same donor name, a different company number
    try:
        for name in names:
            for page in range(MAX_PAGES):
                params = {"query": query_for(name), "rows": PAGE_SIZE, "start": page * PAGE_SIZE,
                          "sort": "AcceptedDate", "order": "desc"}
                data = json.loads(cached_get(http, cache, SEARCH_URL, params))
                results = data.get("Result") or []
                for row in results:
                    if row.get("DonorStatus") != "Company":
                        continue  # individual donors are never read
                    number = _number(row.get("CompanyRegistrationNumber"))
                    same_name = _compact(row.get("DonorName") or "") in legal_names
                    if own_number and number == own_number:
                        rows[row["Id"]] = (row, "company_number")
                    elif not number and same_name:
                        rows.setdefault(row["Id"], (row, "name"))
                    elif same_name:
                        other_numbers[row["Id"]] = number
                total = data.get("Total") or 0
                if not results or (page + 1) * PAGE_SIZE >= total:
                    break
            else:
                section.notes.append(f"more than {MAX_PAGES * PAGE_SIZE} register entries match "
                                     f"'{query_for(name)}'; only the newest were checked")
    except (httpx.HTTPError, ValueError, KeyError) as e:
        section.notes.append(f"Electoral Commission search failed: {type(e).__name__}")
        return section
    finally:
        if own:
            http.close()

    items = []
    for row, matched_by in rows.values():
        ref = row.get("ECRef") or ""
        url = ENTRY_URL.format(ref=ref) if ref else None
        items.append(PoliticalDonation(
            recipient=(row.get("RegulatedEntityName") or "").strip() or "unknown",
            recipient_type=(row.get("RegulatedDoneeType") if row.get("RegulatedEntityType") == "Regulated Donee"
                            else row.get("RegulatedEntityType")) or "unknown",
            to_individual=is_individual(row),
            accounting_unit=row.get("AccountingUnitName"),
            value=float(row.get("Value") or 0), accepted_on=ec_date(row.get("AcceptedDate")),
            donation_type=row.get("DonationType"), nature=row.get("NatureOfDonation"),
            sponsorship=bool(row.get("IsSponsorship")), register_name=row.get("RegisterName"),
            reference=ref, matched_by=matched_by, url=url,
            sources=[Source(name="electoral_commission", url=url)]))

    items.sort(key=lambda d: d.accepted_on or date.min, reverse=True)
    section.found = len(items)
    section.total_value = round(sum(d.value for d in items), 2) if items else None
    section.by_recipient = by_recipient(items)
    dated = [d.accepted_on for d in items if d.accepted_on]
    section.first_donation, section.last_donation = (min(dated), max(dated)) if dated else (None, None)
    section.items = items[:MAX_ITEMS]
    section.sources = [Source(name="electoral_commission", url="https://search.electoralcommission.org.uk")]
    if len(items) > MAX_ITEMS:
        section.notes.append(f"{len(items)} donations found; the newest {MAX_ITEMS} are listed")
    if other_numbers:
        numbers = ", ".join(sorted(set(other_numbers.values())))
        section.notes.append(
            f"{len(other_numbers)} donation(s) under the same name but company number {numbers} "
            "were left out (possibly a typing error in the register; check before relying on it)")
    if any(d.matched_by == "name" for d in items):
        section.notes.append("some donations were filed without a company number and matched by "
                             "exact name (matched_by=name)")
    return section


def by_recipient(items: list[PoliticalDonation], top: int = 15) -> dict[str, float]:
    totals: Counter[str] = Counter()
    for d in items:
        totals[d.recipient] += d.value
    return {k: round(v, 2) for k, v in totals.most_common(top)}


def fetch_group_donations(companies: list[tuple[str, str, str]], *, cache=None,
                          http: httpx.Client | None = None) -> tuple[list[GroupDonations], list[str]]:
    """Donations by other companies in the group: `companies` is (number, name, relation),
    relation being "parent" or "subsidiary". Returns (those that donated, notes)."""
    own = http is None
    http = http or api_client(timeout=60)
    found: list[GroupDonations] = []
    notes: list[str] = []
    try:
        for number, name, relation in companies[:MAX_GROUP_COMPANIES]:
            result = fetch_donations(number, [name], cache=cache, http=http)
            notes.extend(f"{name}: {n}" for n in result.notes)
            if result.found:
                found.append(GroupDonations(
                    name=name, company_number=number, relation=relation,  # type: ignore[arg-type]
                    found=result.found, total_value=result.total_value,
                    by_recipient=result.by_recipient, first_donation=result.first_donation,
                    last_donation=result.last_donation, items=result.items[:MAX_GROUP_ITEMS],
                    sources=result.sources))
    finally:
        if own:
            http.close()
    if len(companies) > MAX_GROUP_COMPANIES:
        notes.append(f"searched the first {MAX_GROUP_COMPANIES} of {len(companies)} group companies")
    return found, notes


# --- what the accounts say -------------------------------------------------------------------

_SENTENCE_END = re.compile("(?<=[.!?])\\s+(?=[A-Z0-9(£'\"‘“])")
_POLITICAL = re.compile(r"\bpolitical\b", re.I)
_GIVING = re.compile(r"\b(donations?|contributions?|expenditure|gifts?|payments?)\b", re.I)
_MAX_STATEMENT = 600


def _about_politics(text: str) -> bool:
    return bool(_POLITICAL.search(text) and _GIVING.search(text))


def _trim_lines(sentence: str) -> str:
    """Drop lines glued on before the statement (an unpunctuated heading such as
    "Directors' report") while what's left still mentions political giving."""
    lines = [l.strip() for l in sentence.split("\n") if l.strip()]
    # Only a heading-like line goes: the next line must start afresh with a capital. A
    # sentence split across iXBRL tags ("The company made" / "political donations...") stays.
    while (len(lines) > 1 and not _POLITICAL.search(lines[0]) and lines[1][:1].isupper()
           and _about_politics(" ".join(lines[1:]))):
        lines.pop(0)
    return " ".join(lines)


def political_statement(text: str) -> str | None:
    """The sentences of an accounts document about political donations ('The company made
    no political donations during the year.'), or None if it says nothing about them."""
    kept_lines = re.sub(r"[ \t\r\f\v]+", " ", text or "")
    hits: list[str] = []
    for raw in _SENTENCE_END.split(kept_lines):
        sentence = _trim_lines(raw)
        if _about_politics(sentence) and 15 <= len(sentence) <= 500:
            if sentence not in hits:
                hits.append(sentence)
        if sum(len(h) for h in hits) >= _MAX_STATEMENT or len(hits) >= 3:
            break
    statement = " ".join(hits)[:_MAX_STATEMENT]
    if statement.count("‘") != statement.count("’"):
        statement = statement.replace("‘", "")  # stray marks OCR reads from specks
    return statement or None
