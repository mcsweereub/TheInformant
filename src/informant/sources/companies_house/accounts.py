"""Figures from filed accounts.

Most small and medium UK companies file accounts as iXBRL: an XHTML document in
which each figure is tagged with what it means (turnover, net assets...), the
period it belongs to, its unit and scale. Reading those tags gives exact numbers
with no guesswork and no LLM.

Larger companies (groups, PLCs) usually file a PDF only; those figures can't be
read this way, and the profile says so instead of guessing.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from bs4 import BeautifulSoup

from ...schema import FinancialPeriod, Financials
from .client import CompaniesHouseClient, CompaniesHouseError
from .mapping import filing_document_url, source

IXBRL = "application/xhtml+xml"
MAX_FILINGS = 3  # each filing carries this year and last, so 3 filings give ~4 years

# Which tags feed which field. Taxonomy prefixes vary between accounting packages
# (core:, frs-core:, uk-gaap:, ifrs-full:, ns5:...), so only the name after the
# colon is matched. Earlier names in each tuple win when a filing has several.
FIELD_TAGS: dict[str, tuple[str, ...]] = {
    "turnover": ("TurnoverRevenue", "Revenue", "TurnoverGrossOperatingRevenue", "Turnover"),
    "gross_profit": ("GrossProfitLoss",),
    "operating_profit": ("OperatingProfitLoss",),
    "profit_before_tax": ("ProfitLossOnOrdinaryActivitiesBeforeTax", "ProfitLossBeforeTax"),
    "profit_loss": ("ProfitLoss", "ProfitLossForPeriod", "ProfitLossOnOrdinaryActivitiesAfterTax"),
    "fixed_assets": ("FixedAssets", "NoncurrentAssets"),
    "current_assets": ("CurrentAssets",),
    "debtors": ("Debtors",),
    "cash": ("CashBankOnHand", "CashBankInHand", "CashCashEquivalents", "CashAndCashEquivalents"),
    "net_current_assets": ("NetCurrentAssetsLiabilities",),
    "total_assets_less_current_liabilities": ("TotalAssetsLessCurrentLiabilities",),
    "net_assets": ("NetAssetsLiabilities", "NetAssetsLiabilitiesIncludingPensionAssetLiability"),
    "equity": ("Equity", "ShareholderFunds"),
    "employees": ("AverageNumberEmployeesDuringPeriod", "EmployeesTotal"),
}
_TAG_TO_FIELD = {
    tag.lower(): (field, rank) for field, tags in FIELD_TAGS.items() for rank, tag in enumerate(tags)
}
# Number formats where the comma is the decimal mark ("1.234,56").
_COMMA_DECIMAL = {"numcommadecimal", "numdotcomma", "numspacecomma"}


def _local(name: str | None) -> str:
    return (name or "").split(":")[-1].lower()


def _find(tag, local: str):
    return tag.find(lambda t: _local(t.name) == local)


def _find_all(tag, local: str):
    return tag.find_all(lambda t: _local(t.name) == local)


def parse_number(text: str, fmt: str | None, scale: str | None, sign: str | None) -> float | None:
    """Turn a tagged figure's display text into a number."""
    kind = _local(fmt)
    text = text.strip()
    if "dash" in kind or kind in ("nocontent", "fixedzero", "zerodash") or text in ("-", "–", "—"):
        value = 0.0
    else:
        if kind in _COMMA_DECIMAL:
            cleaned = text.replace(".", "").replace(" ", "").replace(",", ".")
        else:
            cleaned = text.replace(",", "").replace(" ", "")
        match = re.search(r"\d+(?:\.\d+)?", cleaned)
        if not match:
            return None
        value = float(match.group())
    try:
        value *= 10 ** int(scale or 0)
    except ValueError:
        pass
    return -value if sign == "-" else value


def parse_ixbrl(document: str | bytes) -> list[dict[str, Any]]:
    """Read headline figures from an iXBRL accounts document.

    Returns up to two periods (this year and the comparative), newest first, as
    dicts ready for FinancialPeriod. Only whole-company totals are used: figures
    broken down by a dimension (per asset class, per director...) are ignored.
    """
    if isinstance(document, bytes):
        document = document.decode("utf-8", errors="replace")
    soup = BeautifulSoup(document, "html.parser")

    # context id -> (period_start, period_end); contexts with dimensions are skipped
    contexts: dict[str, tuple[str | None, str]] = {}
    for ctx in _find_all(soup, "context"):
        segment = _find(ctx, "segment") or _find(ctx, "scenario")
        if segment is not None and segment.find(True) is not None:
            continue
        period = _find(ctx, "period")
        if period is None or not ctx.get("id"):
            continue
        instant, start, end = _find(period, "instant"), _find(period, "startdate"), _find(period, "enddate")
        if instant is not None:
            contexts[ctx["id"]] = (None, instant.get_text(strip=True)[:10])
        elif end is not None:
            contexts[ctx["id"]] = (start.get_text(strip=True)[:10] if start is not None else None,
                                   end.get_text(strip=True)[:10])

    currencies: dict[str, str] = {}
    for unit in _find_all(soup, "unit"):
        measure = _find(unit, "measure")
        if measure is not None and unit.get("id") and "iso4217" in measure.get_text().lower():
            currencies[unit["id"]] = measure.get_text(strip=True).split(":")[-1].upper()

    periods: dict[str, dict[str, Any]] = {}
    ranks: dict[tuple[str, str], int] = {}
    for fact in _find_all(soup, "nonfraction"):
        target = _TAG_TO_FIELD.get(_local(fact.get("name")))
        ctx = contexts.get(fact.get("contextref") or "")
        if target is None or ctx is None:
            continue
        field, rank = target
        start, end = ctx
        value = parse_number(fact.get_text(), fact.get("format"), fact.get("scale"), fact.get("sign"))
        if value is None:
            continue
        period = periods.setdefault(end, {"period_end": end})
        if start and "period_start" not in period:
            period["period_start"] = start
        currency = currencies.get(fact.get("unitref") or "")
        if currency and "currency" not in period:
            period["currency"] = currency
        if ranks.get((end, field), 99) > rank:  # first occurrence of the best-ranked tag wins
            ranks[(end, field)] = rank
            period[field] = int(round(value)) if field == "employees" else value

    def figures(p: dict[str, Any]) -> int:
        return sum(1 for k in p if k in FIELD_TAGS)

    # Opening balances can create a third, nearly empty period: keep real ones only.
    real = [p for p in periods.values() if figures(p) >= 2] or list(periods.values())
    real.sort(key=lambda p: p["period_end"], reverse=True)
    return real[:2]


def _is_accounts_filing(item: dict) -> bool:
    # The accounts category also holds AA01 (change of year end) and similar forms.
    return (item.get("description") or "").startswith("accounts-with") and bool(
        (item.get("links") or {}).get("document_metadata"))


def fetch_financials(client: CompaniesHouseClient, number: str, cache=None,
                     max_filings: int = MAX_FILINGS) -> Financials:
    """Read figures from the company's most recent accounts filings."""
    result = Financials(sources=[source(number, "/filing-history?category=accounts")])
    filings = [f for f in client.filing_history(number, limit=12, category="accounts")
               if _is_accounts_filing(f)]
    if not filings:
        result.notes.append("no accounts have been filed yet")
        return result

    merged: dict[date, FinancialPeriod] = {}
    pdf_only: list[str] = []
    for item in filings[:max_filings]:
        meta_url = item["links"]["document_metadata"]
        made_up = (item.get("description_values") or {}).get("made_up_date") or item.get("date")
        cached = cache.get(meta_url) if cache is not None else None
        if cached is not None:
            periods = cached.get("periods") or []
        else:
            try:
                if IXBRL not in client.document_formats(meta_url):
                    periods = []
                else:
                    periods = parse_ixbrl(client.document_content(meta_url, IXBRL))
            except CompaniesHouseError as e:
                result.notes.append(f"accounts made up to {made_up} could not be read: {e}")
                continue
            if cache is not None:
                cache.put(meta_url, {"company_number": number, "made_up": made_up, "periods": periods})
        if not periods:
            pdf_only.append(str(made_up))
            continue
        for raw in periods:
            try:
                period = FinancialPeriod(**raw, filed_on=item.get("date"),
                                         source_url=filing_document_url(number, item))
            except ValueError:
                continue
            merged.setdefault(period.period_end, period)  # newest filing wins (restatements)

    result.periods = sorted(merged.values(), key=lambda p: p.period_end, reverse=True)
    if pdf_only:
        which = (f"The latest accounts (made up to {pdf_only[0]})" if not result.periods
                 else f"Accounts made up to {', '.join(pdf_only)}")
        result.notes.append(
            f"{which} were filed as a PDF only, so their figures can't be read automatically "
            "(larger companies and groups usually file this way)")
    if result.periods and all(p.turnover is None for p in result.periods):
        result.notes.append(
            "no turnover figure: small companies needn't file a profit and loss account")
    return result
