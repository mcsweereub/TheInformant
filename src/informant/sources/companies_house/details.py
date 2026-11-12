"""The deeper registry steps, run after the core lookup: filed accounts, share
capital, charges, ownership, related companies, then risk flags.

Each step is independent: if one fails (a document won't download, an endpoint
errors), its section gets a note and the rest still run. Only a rejected API
key stops everything.
"""

from __future__ import annotations

from ...errors import ConfigError
from ...progress import ProgressFn, emit
from ...schema import Capital, Charges, CompanyProfile, Financials, Ownership, RelatedCompanies
from .accounts import fetch_financials
from .capital import fetch_capital, fetch_charges
from .client import CompaniesHouseClient, CompaniesHouseError
from .mapping import RawCompany
from .ownership import trace_ownership
from .related import fetch_related
from .risk import compute_risk

STAGE = "registry_details"


def add_registry_details(client: CompaniesHouseClient, profile: CompanyProfile, raw: RawCompany,
                         *, progress: ProgressFn | None = None, accounts_cache=None) -> None:
    """Fill financials, capital, charges, ownership and related_companies in place."""
    number = profile.identity.company_number or ""
    name = profile.identity.name
    emit(progress, STAGE, "started", "Accounts, share capital, charges, ownership, related companies")

    def step(label: str, empty, run):
        emit(progress, STAGE, "progress", label)
        try:
            return run()
        except ConfigError:
            raise
        except CompaniesHouseError as e:
            section = empty()
            section.notes.append(f"could not be read: {e}")
            return section

    profile.financials = step("Reading filed accounts", Financials,
                              lambda: fetch_financials(client, number, accounts_cache))
    profile.capital = step("Share capital filings", Capital, lambda: fetch_capital(client, number))
    # Always ask: the profile's has_charges field is unreliable (false for companies
    # that do have charges registered).
    profile.charges = step("Charges", Charges, lambda: fetch_charges(client, number))
    if not profile.charges.notes:
        profile.identity.has_charges = profile.charges.total > 0
    profile.ownership = step("Tracing ownership", Ownership,
                             lambda: trace_ownership(client, number, name, raw.pscs))
    profile.related_companies = step("Directors' other companies", RelatedCompanies,
                                     lambda: fetch_related(client, number, raw.officers))

    emit(progress, STAGE, "done", None,
         periods=len(profile.financials.periods),
         capital_events=len(profile.capital.events),
         charges=profile.charges.total,
         parents=len(profile.ownership.parents),
         related=len(profile.related_companies.companies))


def add_risk_flags(profile: CompanyProfile, raw: RawCompany | None) -> None:
    profile.risk = compute_risk(profile, raw)
