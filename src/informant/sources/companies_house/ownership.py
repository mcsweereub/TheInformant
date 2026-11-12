"""Who owns the company: corporate owners traced upward through the register of
persons with significant control (PSC), until the chain reaches people, a
company registered outside the UK, or a company with no registered controller.
"""

from __future__ import annotations

import re

from ...schema import Controller, Ownership, ParentCompany
from .client import CompaniesHouseClient, CompaniesHouseError, CompanyNotFoundError, normalise_company_number
from .mapping import source

MAX_DEPTH = 6
MAX_LOOKUPS = 12  # companies looked up while tracing, to bound API calls

_NOISE = re.compile(r"\b(limited|ltd|plc|llp|lp|the|group|holdings?|company|co|uk)\b|[^a-z0-9 ]")


def _is_active(psc: dict) -> bool:
    return not psc.get("ceased_on") and not psc.get("ceased")


def _is_company(psc: dict) -> bool:
    return "corporate-entity" in (psc.get("kind") or "")


_UK_WORDS = ("england", "wales", "scotland", "northern ireland", "united kingdom",
             "great britain", "companies house", "uk")


def _may_be_uk_registered(ident: dict) -> bool:
    """False only when the PSC record clearly names a non-UK register, so a
    foreign number isn't mistaken for a UK one."""
    stated = " ".join(str(ident.get(k) or "") for k in
                      ("country_registered", "place_registered", "legal_authority")).lower()
    return not stated.strip() or any(w in stated for w in _UK_WORDS)


def _same_company(a: str, b: str) -> bool:
    """Loose name check, so a foreign registration number that happens to look
    like a UK one isn't matched to an unrelated UK company."""
    ta = set(_NOISE.sub(" ", a.lower()).split())
    tb = set(_NOISE.sub(" ", b.lower()).split())
    return bool(ta & tb) or not ta or not tb


def trace_ownership(client: CompaniesHouseClient, number: str, name: str,
                    pscs: list[dict]) -> Ownership:
    own = Ownership(sources=[source(number, "/persons-with-significant-control")])
    if not any(_is_active(p) for p in pscs):
        own.notes.append("no person with significant control is registered (nobody holds "
                         "more than 25%, or the company is listed)")
        return own

    visited = {number}
    lookups = 0
    queue: list[tuple[int, str, str, list[dict]]] = [(1, name, number, pscs)]
    while queue:
        level, child_name, child_number, child_pscs = queue.pop(0)
        for psc in child_pscs:
            if not _is_active(psc):
                continue
            if not _is_company(psc):
                own.controllers.append(Controller(
                    name=psc.get("name", ""), kind=psc.get("kind", ""),
                    natures_of_control=psc.get("natures_of_control") or [],
                    of_company=child_name, of_company_number=child_number))
                continue

            ident = psc.get("identification") or {}
            try:
                parent = ParentCompany(
                    level=level, name=psc.get("name", ""), controls=child_name,
                    natures_of_control=psc.get("natures_of_control") or [],
                    legal_form=ident.get("legal_form"),
                    country_registered=ident.get("country_registered"),
                    notified_on=psc.get("notified_on"))
            except ValueError:
                continue
            own.parents.append(parent)

            reg = normalise_company_number(ident.get("registration_number") or "")
            if not reg or not _may_be_uk_registered(ident):
                parent.is_ultimate = True  # not on the UK register: the trail ends here
                continue
            if reg in visited:
                continue
            if level >= MAX_DEPTH or lookups >= MAX_LOOKUPS:
                own.notes.append(f"stopped tracing at {parent.name} (lookup limit reached)")
                continue
            visited.add(reg)
            lookups += 1
            try:
                profile = client.profile(reg)
                if not _same_company(parent.name, profile.get("company_name", "")):
                    parent.is_ultimate = True  # number matched a different UK company
                    continue
                parent.company_number = reg
                parent.status = profile.get("company_status")
                parent_pscs = client.pscs(reg)
            except CompanyNotFoundError:
                parent.is_ultimate = True
                continue
            except CompaniesHouseError as e:
                own.notes.append(f"could not look up {parent.name}: {e}")
                continue
            if any(_is_active(p) and _is_company(p) for p in parent_pscs):
                queue.append((level + 1, parent.name, reg, parent_pscs))
            else:
                parent.is_ultimate = True
                if any(_is_active(p) for p in parent_pscs):  # people above the top company
                    queue.append((level + 1, parent.name, reg, parent_pscs))

    own.parents.sort(key=lambda p: p.level)
    return own
