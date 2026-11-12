"""Core registry data: raw API JSON -> identity, classification, people, filings."""

from __future__ import annotations

from dataclasses import dataclass, field

from ...schema import (
    Classification,
    Compliance,
    Filing,
    Filings,
    Identity,
    Location,
    Officer,
    People,
    PreviousName,
    SicCode,
    SignificantControl,
    Source,
)
from .client import PUBLIC_URL, CompaniesHouseClient

# How many filings to pull for analysis (risk flags look back over these); only
# the newest `filings_limit` are listed in the profile.
ANALYSIS_FILINGS = 100


@dataclass
class RawCompany:
    """The API responses as received, kept so later steps don't refetch them."""

    profile: dict
    officers: list[dict] = field(default_factory=list)
    pscs: list[dict] = field(default_factory=list)
    filings: list[dict] = field(default_factory=list)  # newest first, up to ANALYSIS_FILINGS


@dataclass
class CompaniesHouseData:
    identity: Identity
    classification: Classification
    locations: list[Location]
    people: People
    filings: Filings
    raw: RawCompany | None = None


def source(number: str, suffix: str = "") -> Source:
    return Source(name="companies_house", url=f"{PUBLIC_URL}/company/{number}{suffix}")


def filing_document_url(number: str, item: dict) -> str | None:
    """Public link to a filing's PDF on the Companies House website."""
    tid = item.get("transaction_id")
    if not tid:
        return None
    return f"{PUBLIC_URL}/company/{number}/filing-history/{tid}/document?format=pdf&download=0"


def filing_description(item: dict) -> str | None:
    """Filing descriptions are template keys; make them readable."""
    desc = item.get("description")
    if not desc:
        return None
    values = item.get("description_values") or {}
    if "description" in values:  # legacy filings carry free text here
        return values["description"]
    text = desc.replace("-", " ")
    extras = [str(v) for k, v in values.items() if k in ("made_up_date", "officer_name")]
    return f"{text} ({', '.join(extras)})" if extras else text


def map_companies_house(
    profile: dict,
    officers: list[dict],
    pscs: list[dict],
    filings: list[dict],
    filings_limit: int | None = None,
) -> CompaniesHouseData:
    number = profile["company_number"]

    identity = Identity(
        name=profile.get("company_name", ""),
        company_number=number,
        status=profile.get("company_status"),
        company_type=profile.get("type"),
        jurisdiction=profile.get("jurisdiction"),
        incorporated_on=profile.get("date_of_creation"),
        dissolved_on=profile.get("date_of_cessation"),
        previous_names=[
            PreviousName(
                name=p.get("name", ""),
                effective_from=p.get("effective_from"),
                ceased_on=p.get("ceased_on"),
            )
            for p in profile.get("previous_company_names") or []
        ],
        has_charges=profile.get("has_charges"),
        has_insolvency_history=profile.get("has_insolvency_history"),
        sources=[source(number)],
    )

    classification = Classification(
        sic_codes=[SicCode(code=c) for c in profile.get("sic_codes") or []],
        sources=[source(number)],
    )

    locations: list[Location] = []
    roa = profile.get("registered_office_address")
    if roa:
        lines = [roa.get(k) for k in ("premises", "address_line_1", "address_line_2")]
        locations.append(
            Location(
                kind="registered_office",
                address_lines=[l for l in lines if l],
                locality=roa.get("locality"),
                region=roa.get("region"),
                postal_code=roa.get("postal_code"),
                country=roa.get("country"),
                sources=[source(number)],
            )
        )

    people = People(
        officers=[
            Officer(
                name=o.get("name", ""),
                role=o.get("officer_role", ""),
                appointed_on=o.get("appointed_on"),
                resigned_on=o.get("resigned_on"),
                occupation=o.get("occupation"),
                nationality=o.get("nationality"),
                country_of_residence=o.get("country_of_residence"),
            )
            for o in officers
        ],
        significant_control=[
            SignificantControl(
                name=p.get("name", ""),
                kind=p.get("kind", ""),
                natures_of_control=p.get("natures_of_control") or [],
                notified_on=p.get("notified_on"),
                ceased_on=p.get("ceased_on"),
                nationality=p.get("nationality"),
                country_of_residence=p.get("country_of_residence"),
            )
            for p in pscs
        ],
        sources=[source(number, "/officers"), source(number, "/persons-with-significant-control")],
    )

    accounts = profile.get("accounts") or {}
    last_accounts = accounts.get("last_accounts") or {}
    conf = profile.get("confirmation_statement") or {}
    listed = filings if filings_limit is None else filings[:filings_limit]
    filings_section = Filings(
        compliance=Compliance(
            accounts_next_due=accounts.get("next_due"),
            accounts_overdue=accounts.get("overdue"),
            last_accounts_made_up_to=last_accounts.get("made_up_to"),
            last_accounts_type=last_accounts.get("type"),
            confirmation_statement_next_due=conf.get("next_due"),
            confirmation_statement_overdue=conf.get("overdue"),
        ),
        recent=[
            Filing(
                date=f.get("date"),
                category=f.get("category"),
                type=f.get("type"),
                description=filing_description(f),
                document_url=filing_document_url(number, f),
            )
            for f in listed
        ],
        sources=[source(number, "/filing-history")],
    )

    return CompaniesHouseData(
        identity, classification, locations, people, filings_section,
        raw=RawCompany(profile=profile, officers=officers, pscs=pscs, filings=filings),
    )


def fetch_company(client: CompaniesHouseClient, number: str, filings_limit: int = 25) -> CompaniesHouseData:
    return map_companies_house(
        client.profile(number),
        client.officers(number),
        client.pscs(number),
        client.filing_history(number, limit=max(filings_limit, ANALYSIS_FILINGS)),
        filings_limit=filings_limit,
    )
