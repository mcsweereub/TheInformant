"""Flags computed from registry data. No LLM: every flag is a rule over facts
already in the profile, and carries its evidence.

A flag is a prompt to look closer, not a verdict. Severity:
  high     something is formally wrong (overdue accounts, insolvency, strike-off notice)
  warning  a pattern often seen before trouble (director exodus, shrinking balance sheet)
  info     worth knowing, good or bad (new shares issued, headcount growth, sole director)
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from ...schema import CompanyProfile, Risk, RiskFlag
from .mapping import RawCompany

_DISTRESS_STATUSES = {"liquidation", "administration", "receivership",
                      "insolvency-proceedings", "voluntary-arrangement"}
_CLOSED_STATUSES = {"dissolved", "converted-closed", "removed", "closed"}
# Filing deadlines were extended during the pandemic; don't judge lateness before this.
_LATE_CHECK_FROM = date(2021, 7, 1)
# The deadline runs from the official year end, but accounts may be made up to a date
# up to 7 days either side of it (e.g. the nearest Saturday), and the register's filing
# date can lag receipt. So a few days over isn't a signal; only flag clear lateness.
LATE_TOLERANCE_DAYS = 14


def _add_months(d: date, months: int) -> date:
    year, month = divmod(d.month - 1 + months, 12)
    year += d.year
    month += 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def _parse(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value[:10]) if value else None
    except ValueError:
        return None


def _pct(new: float, old: float) -> int:
    return round((new - old) / abs(old) * 100)


def _money(v: float) -> str:
    return f"£{v:,.0f}"


def compute_risk(profile: CompanyProfile, raw: RawCompany | None = None,
                 today: date | None = None) -> Risk:
    today = today or date.today()
    flags: list[RiskFlag] = []

    def flag(code: str, severity: str, message: str, evidence: str | None = None) -> None:
        flags.append(RiskFlag(code=code, severity=severity, message=message, evidence=evidence))

    ident = profile.identity
    comp = profile.filings.compliance
    filings = raw.filings if raw else []
    year_ago, two_years_ago = today - timedelta(days=365), today - timedelta(days=730)

    # --- formal status ----------------------------------------------------
    status = (ident.status or "").lower()
    if status in _DISTRESS_STATUSES:
        flag("status_distress", "high", f"Company status is '{ident.status}'.")
    elif status in _CLOSED_STATUSES:
        flag("status_closed", "high", f"Company status is '{ident.status}'.",
             f"dissolved on {ident.dissolved_on}" if ident.dissolved_on else None)
    if ident.has_insolvency_history:
        flag("insolvency_history", "high", "The company has insolvency history on the register.")
    if comp.accounts_overdue:
        flag("accounts_overdue", "high", "Accounts are overdue.",
             f"were due {comp.accounts_next_due}" if comp.accounts_next_due else None)
    if comp.confirmation_statement_overdue:
        flag("confirmation_statement_overdue", "warning", "The confirmation statement is overdue.",
             f"was due {comp.confirmation_statement_next_due}"
             if comp.confirmation_statement_next_due else None)

    # --- strike-off notices -------------------------------------------------
    gazette = [f for f in filings if (f.get("category") == "gazette"
                                      or (f.get("type") or "").upper().startswith(("GAZ", "DISS")))
               and (_parse(f.get("date")) or date.min) >= two_years_ago]
    if gazette:
        latest = gazette[0]  # newest first
        form = (latest.get("type") or "").upper()
        when = latest.get("date")
        if form.startswith("DISS40"):
            flag("strike_off_discontinued", "warning",
                 "A strike-off notice was issued and later discontinued.", f"discontinued {when}")
        elif form.startswith("GAZ1"):
            flag("strike_off_notice", "high",
                 "First Gazette notice for strike-off: the registrar has started removing the "
                 "company from the register.", f"notice dated {when}")
        elif form.startswith("GAZ2"):
            flag("struck_off", "high", "Final Gazette notice: the company was struck off.",
                 f"dated {when}")

    # --- late filing ----------------------------------------------------------
    deadline_months = 6 if (ident.company_type or "") == "plc" else 9
    late: list[str] = []
    for f in [x for x in filings if (x.get("description") or "").startswith("accounts-with")][:3]:
        made_up = _parse((f.get("description_values") or {}).get("made_up_date"))
        filed = _parse(f.get("date"))
        if not made_up or not filed or made_up < _LATE_CHECK_FROM:
            continue
        if ident.incorporated_on and made_up < _add_months(ident.incorporated_on, 24):
            continue  # first accounts have a different deadline
        days = (filed - _add_months(made_up, deadline_months)).days
        if days > LATE_TOLERANCE_DAYS:
            late.append(f"accounts to {made_up} filed {days} days late")
    if late:
        flag("accounts_filed_late", "warning", "Recent accounts were filed after the deadline.",
             "; ".join(late))

    # --- people ---------------------------------------------------------------
    directors = [o for o in profile.people.officers if "director" in o.role.lower()]
    current = [o for o in directors if o.active]
    resigned = [o for o in directors if o.resigned_on and o.resigned_on >= year_ago]
    if directors and not current and status == "active":
        flag("no_directors", "warning", "No current directors are registered.")
    elif len(current) == 1:
        flag("sole_director", "info", "The company has a single director.")
    if len(resigned) >= 2 and len(resigned) * 3 >= max(len(current), 1):
        flag("director_turnover", "warning",
             f"{len(resigned)} directors resigned in the last 12 months.",
             f"{len(current)} current director(s)")

    # --- changes of address and name ------------------------------------------
    moves = [f for f in filings if (f.get("type") or "").upper() == "AD01"
             and (_parse(f.get("date")) or date.min) >= two_years_ago]
    if len(moves) >= 2:
        flag("address_changes", "warning",
             f"The registered office changed {len(moves)} times in the last 2 years.",
             ", ".join(f.get("date", "") for f in moves))
    renamed = [n for n in ident.previous_names if n.ceased_on and n.ceased_on >= two_years_ago]
    if renamed:
        flag("recent_name_change", "info", "The company changed its name in the last 2 years.",
             f"previously {renamed[0].name} (until {renamed[0].ceased_on})")

    # --- age and activity ------------------------------------------------------
    if ident.incorporated_on and ident.incorporated_on >= year_ago:
        flag("young_company", "info", "Incorporated less than a year ago.",
             f"incorporated {ident.incorporated_on}")
    if (comp.last_accounts_type or "").lower() == "dormant":
        flag("dormant", "info", "The last accounts were dormant accounts (no trading).")

    # --- borrowing ---------------------------------------------------------------
    if profile.charges.outstanding:
        lenders = list(dict.fromkeys(
            name for c in profile.charges.items if c.status != "fully-satisfied" for name in c.lenders))
        flag("outstanding_charges", "info",
             f"{profile.charges.outstanding} outstanding charge(s): assets are pledged to lenders.",
             "lenders: " + "; ".join(lenders[:5]) if lenders else None)

    # --- share capital -------------------------------------------------------------
    allotments = [e for e in profile.capital.events
                  if e.kind == "allotment" and e.event_date and e.event_date >= two_years_ago]
    if allotments:
        flag("recent_share_allotments", "info",
             f"{len(allotments)} share allotment(s) in the last 2 years: new shares were issued, "
             "which often means funding or a share scheme.",
             f"latest on {allotments[0].event_date}")

    # --- finances (from filed accounts) -----------------------------------------------
    periods = profile.financials.periods
    if periods:
        latest = periods[0]
        prev = periods[1] if len(periods) > 1 else None
        if latest.net_assets is not None and latest.net_assets < 0:
            flag("negative_net_assets", "warning",
                 "Liabilities exceed assets on the latest balance sheet.",
                 f"net assets {_money(latest.net_assets)} at {latest.period_end}")
        if prev:
            if (latest.net_assets is not None and prev.net_assets and prev.net_assets > 0
                    and latest.net_assets < prev.net_assets * 0.5):
                flag("net_assets_fall", "warning",
                     f"Net assets fell {abs(_pct(latest.net_assets, prev.net_assets))}% in a year.",
                     f"{_money(prev.net_assets)} to {_money(latest.net_assets)}")
            if (latest.cash is not None and prev.cash and prev.cash > 10_000
                    and latest.cash < prev.cash * 0.5):
                flag("cash_fall", "info",
                     f"Cash fell {abs(_pct(latest.cash, prev.cash))}% in a year.",
                     f"{_money(prev.cash)} to {_money(latest.cash)}")
            if latest.employees is not None and prev.employees and prev.employees >= 8:
                change = _pct(latest.employees, prev.employees)
                if change <= -25:
                    flag("headcount_fall", "info", f"Average headcount fell {abs(change)}% in a year.",
                         f"{prev.employees} to {latest.employees}")
                elif change >= 25:
                    flag("headcount_growth", "info", f"Average headcount grew {change}% in a year.",
                         f"{prev.employees} to {latest.employees}")

    order = {"high": 0, "warning": 1, "info": 2}
    flags.sort(key=lambda f: order[f.severity])
    return Risk(flags=flags)
