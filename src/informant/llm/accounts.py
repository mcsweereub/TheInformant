"""Reading figures and facts from the pages of a company's accounts.

The model copies figures exactly as printed, with the unit shown in the column
header (e.g. "£m"); code converts them and checks every figure appears in the
page text, so a misread or invented number is dropped rather than stored.
Two calls: one for the financial statements, one for the narrative facts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from .backends.base import CompleteFn
from .result import echoed

Unit = Literal["units", "thousands", "millions", "billions"]
SCALE = {"units": 1, "thousands": 1_000, "millions": 1_000_000, "billions": 1_000_000_000}
FIGURE_FIELDS = ("turnover", "gross_profit", "operating_profit", "profit_before_tax",
                 "profit_after_tax", "total_assets", "net_assets", "cash")
MAX_CHARS = 14_000  # per call

FIGURES_PROMPT = """You read pages from a company's annual report and copy out its headline figures for a personal research database. You reply with JSON only.

For the current year and the previous year (the two columns most statements show), give:
period_end: the year-end date as written, e.g. "27 December 2025".
currency: GBP, USD or EUR.
unit: the unit in the column heading: units, thousands (£000 or £'000) or millions (£m).
turnover (revenue; for a bank, total income), gross_profit, operating_profit, profit_before_tax, profit_after_tax, total_assets, net_assets (total equity), cash (cash and cash equivalents).
Copy each figure exactly as printed in its column, keeping brackets for negative numbers, for example "1,509.9" or "(12.3)". If the group (consolidated) and the parent company are both shown, use the group figures. Leave a figure empty if the pages don't show it. Never calculate a figure yourself."""

FACTS_PROMPT = """You read pages from a company's annual report and record facts about the company for a personal research database. You reply with JSON only.

Give, using only what the pages say (leave a value empty if they don't say it):
principal_activities: one sentence on what the company does, as the report states it.
auditor: the name of the audit firm.
ultimate_controlling_party: the organisation the report names as the ultimate parent or controlling party.
employees: the average number of employees in the latest year, as printed.
substantial_shareholders: organisations the report lists as holding notable stakes (for example in a table of substantial shareholdings), each with its stake as printed. Leave out individual people."""


class ExtractedYear(BaseModel):
    period_end: str
    currency: str
    unit: Unit
    turnover: str
    gross_profit: str
    operating_profit: str
    profit_before_tax: str
    profit_after_tax: str
    total_assets: str
    net_assets: str
    cash: str


class FiguresExtraction(BaseModel):
    years: Annotated[list[ExtractedYear], Field(max_length=2)]


class StakeHolder(BaseModel):
    name: str
    stake: str


class FactsExtraction(BaseModel):
    principal_activities: str
    auditor: str
    ultimate_controlling_party: str
    employees: str
    substantial_shareholders: Annotated[list[StakeHolder], Field(max_length=20)]


@dataclass
class AccountsResult:
    periods: list[dict] = field(default_factory=list)  # ready for FinancialPeriod
    principal_activities: str | None = None
    auditor: str | None = None
    ultimate_controlling_party: str | None = None
    shareholders: list[tuple[str, str | None]] = field(default_factory=list)
    model: str | None = None
    notes: list[str] = field(default_factory=list)


# --- numbers -------------------------------------------------------------------------------

_DIGITS = re.compile(r"[^\d.]")
_MONTH_NAMES = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September",
     "October", "November", "December"], 1)}


def parse_figure(text: str) -> float | None:
    """'1,509.9' -> 1509.9; '(12.3)' or '-12.3' -> -12.3; '' or '-' -> None."""
    t = (text or "").strip().replace("£", "").replace("$", "").replace("€", "")
    if not re.search(r"\d", t):
        return None
    negative = t.startswith("(") or t.startswith("-") or t.startswith("−")
    digits = _DIGITS.sub("", t)
    if digits.count(".") > 1 or not digits.strip("."):
        return None
    value = float(digits)
    return -value if negative else value


def figure_in_text(text: str, page_text_compact: str) -> bool:
    """The figure's digits appear in the pages (ignoring commas and spaces)."""
    digits = _DIGITS.sub("", text or "")
    return len(digits.replace(".", "")) >= 2 and digits in page_text_compact


def parse_date(text: str, fallback: date | None) -> date | None:
    t = (text or "").strip()
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", t)
    if m and m.group(2).lower()[:3] in {k[:3] for k in _MONTH_NAMES}:
        month = next(v for k, v in _MONTH_NAMES.items() if k[:3] == m.group(2).lower()[:3])
        try:
            return date(int(m.group(3)), month, int(m.group(1)))
        except ValueError:
            pass
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return fallback


_OCR_SUFFIXES = {"Ple": "plc", "PIc": "plc", "Pic": "plc", "LLF": "LLP", "Lid": "Ltd", "Ltd.": "Ltd"}


def fix_ocr_name(name: str) -> str:
    """Common OCR slips in company suffixes ("J.P. Morgan Securities Ple" -> "plc")."""
    words = name.split()
    if words and words[-1] in _OCR_SUFFIXES:
        words[-1] = _OCR_SUFFIXES[words[-1]]
    return " ".join(words)


def _pages_text(pages, topics: tuple[str, ...] | None) -> str:
    chosen = [p for p in pages if topics is None or set(p.topics) & set(topics)] or list(pages)
    parts, total = [], 0
    for p in chosen:
        block = f"=== PAGE {p.number} ({', '.join(p.topics)})\n{p.text.strip()}"
        if total + len(block) > MAX_CHARS:
            block = block[:max(0, MAX_CHARS - total)]
        parts.append(block)
        total += len(block)
        if total >= MAX_CHARS:
            break
    return "\n\n".join(parts)


# --- extraction ------------------------------------------------------------------------------


def extract_accounts(company_name: str, pages, made_up: date | None,
                     complete: CompleteFn) -> AccountsResult:
    result = AccountsResult()
    if not pages:
        return result
    figure_text = _pages_text(pages, ("income_statement", "balance_sheet", "cash_flow", "highlights"))
    fact_text = _pages_text(pages, ("employees", "activities", "shareholders", "parent", "auditor"))
    # Page text without commas and spaces, so "1,509.9" and "1509.9" both match.
    compact_all = re.sub(r"[,\s]", "", "\n".join(p.text for p in pages))

    c = complete(FIGURES_PROMPT,
                 f"Company: {company_name}\n\nPages:\n\n{figure_text}\n\n=== END OF PAGES\n\n"
                 "Now copy out the headline figures as JSON.", FiguresExtraction)
    result.model = c.model
    if c.data is None:
        result.notes.append(f"figures not read: {c.notes[-1] if c.notes else 'no result'}")
    else:
        dropped = 0
        seen_ends: set[date] = set()
        for i, year in enumerate(c.data.years):
            fallback = made_up if i == 0 else (date(made_up.year - 1, made_up.month, min(made_up.day, 28))
                                               if made_up else None)
            end = parse_date(year.period_end, fallback)
            if end is None or end in seen_ends:
                continue
            seen_ends.add(end)
            scale = SCALE.get(year.unit, 1)
            period: dict = {"period_end": end.isoformat(),
                            "currency": (year.currency or "GBP").upper()[:3]}
            for name in FIGURE_FIELDS:
                raw = getattr(year, name)
                value = parse_figure(raw)
                if value is None:
                    continue
                if not figure_in_text(raw, compact_all):
                    dropped += 1
                    continue
                key = "profit_loss" if name == "profit_after_tax" else name
                period[key] = value * scale
            if len(period) > 2:
                result.periods.append(period)
        if dropped:
            result.notes.append(f"dropped {dropped} figure(s) that don't appear on the pages")

    if fact_text.strip():
        f = complete(FACTS_PROMPT,
                     f"Company: {company_name}\n\nPages:\n\n{fact_text}\n\n=== END OF PAGES\n\n"
                     "Now record the facts as JSON.", FactsExtraction)
        if f.data is None:
            result.notes.append(f"facts not read: {f.notes[-1] if f.notes else 'no result'}")
        else:
            d = f.data
            tokens = set(re.findall(r"[a-z]{3,}", " ".join(p.text for p in pages).lower()))

            def grounded(value: str, share: float) -> str | None:
                """Whole words must appear on the pages: all of a name's words, most of
                a sentence's ("holdings" doesn't count just because "shareholdings" does)."""
                v = " ".join((value or "").split())
                if not v or echoed(v) or v.lower() in ("unknown", "none", "n/a", "not stated"):
                    return None
                words = re.findall(r"[a-z]{3,}", v.lower())
                return v if words and sum(w in tokens for w in words) / len(words) >= share else None

            # Only trust a stated activity when a "principal activities" page was read;
            # otherwise the model improvises from whatever page it was given.
            has_activities_page = any("activities" in p.topics for p in pages)
            result.principal_activities = (grounded(d.principal_activities, 0.6)
                                           if has_activities_page else None)
            result.auditor = grounded(d.auditor, 1.0)
            result.ultimate_controlling_party = grounded(d.ultimate_controlling_party, 1.0)
            employees = parse_figure(d.employees)
            if employees and figure_in_text(d.employees, compact_all) and result.periods:
                result.periods[0]["employees"] = int(employees)
            for s in d.substantial_shareholders:
                name = grounded(s.name, 1.0)
                if name:
                    # A stake must be a figure ("5.15%"), not a word like "notable".
                    stake = s.stake.strip() if re.search(r"\d", s.stake or "") else None
                    result.shareholders.append((fix_ocr_name(name), stake))
    return result
