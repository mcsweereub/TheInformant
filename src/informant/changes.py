"""What changed between two lookups of the same company.

Each save keeps a dated copy of the profile (storage.py, `history/<number>/`). `compare`
reads two of them section by section and lists the changes that mean something: a new
director or a resignation, a filing, a charge, a risk flag, new figures, a contract,
a donation, a change in open roles, a new subdomain... Timestamps, sources and wording
the LLM varies from run to run are ignored.

A section is compared only if both lookups looked at it. A `--no-web` run has no
website, and that isn't a change: the website simply wasn't checked that time.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from .schema import CompanyProfile

Kind = Literal["added", "removed", "changed"]


class Change(BaseModel):
    section: str  # e.g. "officers", "filings", "risk"
    kind: Kind
    summary: str  # one readable line
    before: str | None = None
    after: str | None = None


class Comparison(BaseModel):
    company_number: str | None
    name: str
    previous_at: datetime  # generated_at of the older lookup
    current_at: datetime
    changes: list[Change]
    not_compared: list[str]  # sections only one of the two lookups looked at


def _money(v: float | None) -> str:
    if v is None:
        return "n/a"
    sign, v = ("-" if v < 0 else ""), abs(v)
    if v >= 1_000_000:
        return f"{sign}£{v / 1_000_000:.2f}m"
    return f"{sign}£{v / 1_000:.0f}k" if v >= 1_000 else f"{sign}£{v:.0f}"


class _Differ:
    def __init__(self, old: CompanyProfile, new: CompanyProfile) -> None:
        self.old, self.new = old, new
        self.changes: list[Change] = []
        self.not_compared: list[str] = []

    def add(self, section: str, kind: Kind, summary: str, before: Any = None, after: Any = None) -> None:
        self.changes.append(Change(section=section, kind=kind, summary=summary,
                                   before=None if before is None else str(before),
                                   after=None if after is None else str(after)))

    def checked(self, section: str, looked_at: Callable[[CompanyProfile], Any]) -> bool:
        """True if both lookups looked at the section; otherwise it's noted as not compared."""
        if looked_at(self.old) and looked_at(self.new):
            return True
        if looked_at(self.old) or looked_at(self.new):
            self.not_compared.append(section)
        return False

    def value(self, section: str, label: str, get: Callable[[CompanyProfile], Any],
              show: Callable[[Any], str] = str) -> None:
        before, after = get(self.old), get(self.new)
        if before != after and not (before in (None, "", []) and after in (None, "", [])):
            b = show(before) if before not in (None, "") else "none"
            a = show(after) if after not in (None, "") else "none"
            self.add(section, "changed", f"{label}: {b} -> {a}", b, a)

    def items(self, section: str, get: Callable[[CompanyProfile], Iterable],
              key: Callable[[Any], Any], show: Callable[[Any], str], *,
              added: str = "new", removed: str = "gone", report_removed: bool = True) -> None:
        old = {key(x): x for x in get(self.old)}
        new = {key(x): x for x in get(self.new)}
        for k, x in new.items():
            if k not in old:
                self.add(section, "added", f"{added}: {show(x)}", after=show(x))
        if report_removed:
            for k, x in old.items():
                if k not in new:
                    self.add(section, "removed", f"{removed}: {show(x)}", before=show(x))


def compare(old: CompanyProfile, new: CompanyProfile) -> Comparison:
    """The changes from `old` to `new` (two lookups of the same company)."""
    d = _Differ(old, new)

    # --- identity and the register --------------------------------------------------------
    d.value("identity", "name", lambda p: p.identity.name)
    d.value("identity", "status", lambda p: p.identity.status)
    d.value("identity", "company type", lambda p: p.identity.company_type)
    d.value("identity", "insolvency history", lambda p: p.identity.has_insolvency_history)

    def office(p: CompanyProfile) -> str | None:
        loc = next((l for l in p.locations if l.kind == "registered_office"), None)
        return ", ".join(loc.address_lines + [loc.postal_code or ""]).strip(", ") if loc else None
    d.value("identity", "registered office", office)
    d.items("identity", lambda p: p.classification.sic_codes, lambda s: s.code,
            lambda s: f"SIC {s.code} {s.description or ''}".strip(), added="new SIC code",
            removed="SIC code removed")

    # Officers: appointments and resignations (resigned officers stay on the register).
    def officers(p: CompanyProfile) -> dict:
        return {(o.name, o.role): o for o in p.people.officers}
    old_off, new_off = officers(old), officers(new)
    for k, o in new_off.items():
        before = old_off.get(k)
        if before is None:
            if o.active:
                d.add("officers", "added", f"appointed: {o.name} ({o.role}"
                      + (f", {o.appointed_on}" if o.appointed_on else "") + ")", after=o.name)
        elif before.active and not o.active:
            d.add("officers", "removed", f"resigned: {o.name} ({o.role}, {o.resigned_on})", before=o.name)
    d.items("people", lambda p: [s for s in p.people.significant_control if not s.ceased_on],
            lambda s: s.name, lambda s: f"{s.name} ({s.kind})", added="new person with significant "
            "control", removed="no longer has significant control")

    def compliance(p: CompanyProfile):
        return p.filings.compliance
    d.value("filings", "accounts overdue", lambda p: compliance(p).accounts_overdue)
    d.value("filings", "confirmation statement overdue",
            lambda p: compliance(p).confirmation_statement_overdue)
    d.value("filings", "next accounts due", lambda p: compliance(p).accounts_next_due)
    d.items("filings", lambda p: p.filings.recent,
            lambda f: (f.date, f.type, f.description), lambda f: f"{f.date} {f.description or f.type}",
            added="new filing", report_removed=False)  # old filings drop off the recent list

    # --- registry depth --------------------------------------------------------------------
    if d.checked("financials", lambda p: p.financials.sources):
        old_periods = {p.period_end: p for p in old.financials.periods}
        for period in new.financials.periods:
            before = old_periods.get(period.period_end)
            if before is None:
                d.add("financials", "added", f"accounts for {period.period_end}: turnover "
                      f"{_money(period.turnover)}, profit {_money(period.profit_loss)}, net assets "
                      f"{_money(period.net_assets)}, employees {period.employees if period.employees is not None else 'n/a'}")
                continue
            for field in ("turnover", "profit_loss", "net_assets", "cash", "employees"):
                b, a = getattr(before, field), getattr(period, field)
                if b != a and not (b is None or a is None):  # a restatement
                    d.add("financials", "changed", f"{period.period_end} {field.replace('_', ' ')} "
                          f"restated: {b} -> {a}", b, a)
        d.value("financials", "auditor", lambda p: p.financials.auditor)
    if d.checked("charges", lambda p: p.charges.sources):
        d.items("charges", lambda p: p.charges.items, lambda c: (c.charge_number, c.created_on),
                lambda c: f"{c.created_on} {c.classification or 'charge'}"
                + (f" to {', '.join(c.lenders)}" if c.lenders else ""), added="new charge",
                report_removed=False)
        old_status = {(c.charge_number, c.created_on): c.status for c in old.charges.items}
        for c in new.charges.items:
            before = old_status.get((c.charge_number, c.created_on))
            if before and before != c.status:
                d.add("charges", "changed", f"charge {c.created_on} {c.classification or ''}: "
                      f"{before} -> {c.status}".replace("  ", " "), before, c.status)
    if d.checked("capital", lambda p: p.capital.sources):
        d.items("capital", lambda p: p.capital.events, lambda e: (e.form, e.event_date, e.filed_on),
                lambda e: f"{e.event_date or e.filed_on} {e.kind}" + (f" ({e.description})" if e.description else ""),
                added="new share capital filing", report_removed=False)
    if d.checked("ownership", lambda p: p.ownership.sources):
        def ultimate(p: CompanyProfile) -> str | None:
            return next((x.name for x in p.ownership.parents if x.is_ultimate), None)
        d.value("ownership", "ultimate parent", ultimate)
        d.value("ownership", "direct owner",
                lambda p: next((x.name for x in p.ownership.parents if x.level == 1), None))
    d.items("risk", lambda p: p.risk.flags, lambda f: f.code, lambda f: f"[{f.severity}] {f.message}",
            added="new flag", removed="flag cleared")

    # --- other registers and records -----------------------------------------------------------
    if d.checked("lei", lambda p: p.lei.lei or p.lei.notes):
        d.value("lei", "LEI", lambda p: p.lei.lei)
        d.value("lei", "LEI status", lambda p: p.lei.registration_status)
        d.value("lei", "LEI parent", lambda p: (p.lei.ultimate_parent or p.lei.direct_parent or None)
                and (p.lei.ultimate_parent or p.lei.direct_parent).name)
    if d.checked("public_contracts", lambda p: p.public_contracts.searched_for):
        d.items("public_contracts", lambda p: p.public_contracts.items, lambda c: c.url or c.title,
                lambda c: f"{c.awarded_on} {c.buyer}: {c.title[:80]}"
                + (f" ({_money(c.value)})" if c.value else ""), added="new contract award",
                report_removed=False)
    if d.checked("political_donations", lambda p: p.political_donations.searched_for):
        d.items("political_donations", lambda p: p.political_donations.items, lambda x: x.reference,
                lambda x: f"{x.accepted_on} {_money(x.value)} to {x.recipient}",
                added="new donation", report_removed=False)
        d.value("political_donations", "accounts statement",
                lambda p: p.political_donations.accounts_statement)
    if d.checked("property", lambda p: p.property.dataset):
        d.value("property", "titles owned", lambda p: p.property.found)
        # titles one by one only when every title is listed in both lookups (big holders
        # have a capped list, so a title dropping off it isn't a sale)
        if all(len(p.property.items) == p.property.found for p in (old, new)):
            d.items("property", lambda p: p.property.items, lambda t: t.title_number,
                    lambda t: f"{t.tenure or 'title'} {t.address or t.title_number}"[:120],
                    added="new title", removed="title no longer registered to it")
        old_g = {g.company_number: g for g in old.property.group}
        new_g = {g.company_number: g for g in new.property.group}
        for number in list(new_g) + [n for n in old_g if n not in new_g]:
            g = new_g.get(number) or old_g[number]
            b = old_g[number].titles if number in old_g else 0
            a = new_g[number].titles if number in new_g else 0
            if b != a:
                d.add("property", "changed", f"titles owned by {g.relation} {g.name}: {b} -> {a}", b, a)
    if d.checked("hiring", lambda p: p.hiring.provider or p.hiring.notes):
        d.value("hiring", "open roles", lambda p: p.hiring.total if p.hiring.provider else None)
        old_depts, new_depts = old.hiring.departments, new.hiring.departments
        for dept in sorted(set(old_depts) | set(new_depts)):
            b, a = old_depts.get(dept, 0), new_depts.get(dept, 0)
            if b != a and max(a, b) >= 3 and abs(a - b) >= max(2, b // 2):  # big moves only
                d.add("hiring", "changed", f"{dept} roles: {b} -> {a}", b, a)
    if d.checked("domain", lambda p: p.domain.name):
        d.value("domain", "email provider", lambda p: p.domain.email_provider)
        d.items("domain", lambda p: p.domain.subdomains, lambda s: s, str, added="new subdomain",
                report_removed=False)  # certificates expire; a missing subdomain means little
        d.items("domain", lambda p: p.domain.other_domains, lambda s: s, str,
                added="new domain on its certificates", report_removed=False)
        d.items("domain", lambda p: p.domain.verified_services + p.domain.email_services,
                lambda s: s, str, added="started using", removed="no longer listed")

    # --- website and what the LLM read -------------------------------------------------------
    if d.checked("website", lambda p: p.website.url):
        d.value("website", "website", lambda p: p.website.url)
        d.items("website", lambda p: p.tech_stack, lambda t: t, str, added="new technology",
                removed="technology no longer seen")
        d.items("website", lambda p: p.website.analytics_ids, lambda t: t, str,
                added="new analytics ID", removed="analytics ID gone")
    if d.checked("products", lambda p: p.website.llm_model):
        # LLM answers vary between runs, so only names are compared (case-insensitive).
        d.items("products", lambda p: p.products, lambda x: x.name.lower(), lambda x: x.name,
                added="new product", removed="product no longer listed")
        d.items("customers", lambda p: p.customers, lambda x: x.name.lower(), lambda x: x.name,
                added="new customer", removed="customer no longer named")
    if d.checked("news", lambda p: p.news.window_days):
        d.items("news", lambda p: [n for n in p.news.items if n.relevant is not False],
                lambda n: n.url, lambda n: f"{n.published_at.date() if n.published_at else ''} {n.title}".strip(),
                added="news", report_removed=False)

    return Comparison(company_number=new.identity.company_number, name=new.identity.name,
                      previous_at=old.generated_at, current_at=new.generated_at,
                      changes=d.changes, not_compared=d.not_compared)
