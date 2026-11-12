"""Output schema: one CompanyProfile JSON document per company.

This file is the contract with downstream consumers (e.g. a visualisation
program). Rules:
  * Every section is always present, even when empty, so consumers never
    need to check whether a key exists.
  * Every section (and every list item that comes from a different place)
    carries `sources`, saying where and when the data was retrieved.
  * Bump SCHEMA_VERSION whenever a field is renamed/removed or its meaning
    changes. Adding optional fields is a minor bump.
"""

from __future__ import annotations

import datetime as dt
from datetime import date, datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = "0.3.0"

SourceName = Literal[
    "companies_house",
    "website",
    "news",
    "jobs",
    "llm_extraction",
    "other",
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class Source(BaseModel):
    """Provenance for a piece of data."""

    name: SourceName
    url: str | None = None
    retrieved_at: datetime = Field(default_factory=utcnow)


class Sourced(BaseModel):
    sources: list[Source] = Field(default_factory=list)


# --- identity -------------------------------------------------------------


class PreviousName(BaseModel):
    name: str
    effective_from: date | None = None
    ceased_on: date | None = None


class Identity(Sourced):
    name: str
    company_number: str | None = None
    status: str | None = None  # active, dissolved, liquidation, ...
    company_type: str | None = None  # ltd, plc, llp, ...
    jurisdiction: str | None = None
    incorporated_on: date | None = None
    dissolved_on: date | None = None
    website: str | None = None
    description: str | None = None  # short plain-English summary (LLM, from website)
    previous_names: list[PreviousName] = Field(default_factory=list)
    has_charges: bool | None = None
    has_insolvency_history: bool | None = None


# --- classification -------------------------------------------------------


class SicCode(BaseModel):
    code: str
    description: str | None = None


class Classification(Sourced):
    sic_codes: list[SicCode] = Field(default_factory=list)
    sectors: list[str] = Field(default_factory=list)  # broad, human-friendly
    tags: list[str] = Field(default_factory=list)  # free-form keywords


# --- locations ------------------------------------------------------------


class Location(Sourced):
    kind: Literal["registered_office", "office", "site", "other"] = "other"
    address_lines: list[str] = Field(default_factory=list)
    locality: str | None = None
    region: str | None = None
    postal_code: str | None = None
    country: str | None = None


# --- people (official registries only) ------------------------------------


class Officer(BaseModel):
    """A director/secretary/member from Companies House.

    Deliberately minimal: no addresses or dates of birth are stored.
    """

    name: str
    role: str
    appointed_on: date | None = None
    resigned_on: date | None = None
    occupation: str | None = None
    nationality: str | None = None
    country_of_residence: str | None = None

    @property
    def active(self) -> bool:
        return self.resigned_on is None


class SignificantControl(BaseModel):
    """A person or entity with significant control (PSC)."""

    name: str
    kind: str  # individual / corporate-entity / legal-person ...
    natures_of_control: list[str] = Field(default_factory=list)
    notified_on: date | None = None
    ceased_on: date | None = None
    nationality: str | None = None
    country_of_residence: str | None = None


class People(Sourced):
    officers: list[Officer] = Field(default_factory=list)
    significant_control: list[SignificantControl] = Field(default_factory=list)


# --- filings & financials -------------------------------------------------


class Filing(BaseModel):
    date: dt.date | None = None
    category: str | None = None  # accounts, confirmation-statement, officers...
    type: str | None = None  # form code, e.g. AA, CS01
    description: str | None = None
    document_url: str | None = None


class Compliance(BaseModel):
    accounts_next_due: date | None = None
    accounts_overdue: bool | None = None
    last_accounts_made_up_to: date | None = None
    last_accounts_type: str | None = None  # micro-entity, small, full, ...
    confirmation_statement_next_due: date | None = None
    confirmation_statement_overdue: bool | None = None


class Filings(Sourced):
    compliance: Compliance = Field(default_factory=Compliance)
    recent: list[Filing] = Field(default_factory=list)


class FinancialPeriod(BaseModel):
    period_end: date
    currency: str = "GBP"
    turnover: float | None = None
    profit_loss: float | None = None
    net_assets: float | None = None
    cash: float | None = None
    employees: int | None = None


class Financials(Sourced):
    """Populated later from iXBRL accounts filings."""

    periods: list[FinancialPeriod] = Field(default_factory=list)


# --- website --------------------------------------------------------------


class WebPage(BaseModel):
    url: str
    kind: str  # home, about, products, investors, customers, careers, contact, news, other
    status_code: int | None = None
    title: str | None = None
    text_chars: int = 0  # how much readable text the page had
    rendered: bool = False  # loaded with a headless browser (JavaScript-built page)


class Website(Sourced):
    url: str | None = None
    discovered_via: Literal["user", "wikidata", "domain_guess"] | None = None
    confidence: Literal["high", "medium", "low"] | None = None
    verification: list[str] = Field(default_factory=list)  # why we think it's theirs
    title: str | None = None
    meta_description: str | None = None
    pages: list[WebPage] = Field(default_factory=list)
    emails: list[str] = Field(default_factory=list)  # role addresses only (info@, sales@...)
    phones: list[str] = Field(default_factory=list)
    social_links: dict[str, str] = Field(default_factory=dict)  # company accounts only
    business_model: str | None = None  # B2B / B2C / B2B2C / marketplace ... (LLM)
    target_markets: list[str] = Field(default_factory=list)  # (LLM)
    llm_model: str | None = None  # which model produced the LLM fields, e.g. ollama/gemma4:12b
    notes: list[str] = Field(default_factory=list)  # skipped pages, blocks, warnings


# --- sections filled by later stages --------------------------------------


class Product(Sourced):
    name: str
    description: str | None = None
    category: str | None = None


class NewsItem(Sourced):
    title: str
    url: str
    published_at: datetime | None = None
    publisher: str | None = None
    summary: str | None = None
    sentiment: float | None = None  # -1.0 (negative) .. 1.0 (positive)


class JobPosting(Sourced):
    title: str
    department: str | None = None
    location: str | None = None
    url: str | None = None
    posted_at: date | None = None


class Hiring(Sourced):
    open_roles: list[JobPosting] = Field(default_factory=list)


# --- top level ------------------------------------------------------------


class CompanyProfile(BaseModel):
    schema_version: str = SCHEMA_VERSION
    generated_at: datetime = Field(default_factory=utcnow)
    query: str

    identity: Identity
    classification: Classification = Field(default_factory=Classification)
    locations: list[Location] = Field(default_factory=list)
    people: People = Field(default_factory=People)
    filings: Filings = Field(default_factory=Filings)
    financials: Financials = Field(default_factory=Financials)
    website: Website = Field(default_factory=Website)
    products: list[Product] = Field(default_factory=list)
    tech_stack: list[str] = Field(default_factory=list)
    news: list[NewsItem] = Field(default_factory=list)
    hiring: Hiring = Field(default_factory=Hiring)

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)
