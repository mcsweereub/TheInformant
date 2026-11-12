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

SCHEMA_VERSION = "0.5.0"

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
    """One accounting period, read from the figures tagged in filed accounts (iXBRL).

    Small companies often don't file a profit and loss account, so turnover and
    profit are frequently absent while the balance-sheet figures are present.
    """

    period_end: date
    period_start: date | None = None
    currency: str = "GBP"
    # profit and loss
    turnover: float | None = None
    gross_profit: float | None = None
    operating_profit: float | None = None
    profit_before_tax: float | None = None
    profit_loss: float | None = None  # profit (or loss) for the period, after tax
    # balance sheet
    fixed_assets: float | None = None
    current_assets: float | None = None
    debtors: float | None = None
    cash: float | None = None
    net_current_assets: float | None = None
    total_assets_less_current_liabilities: float | None = None
    net_assets: float | None = None
    equity: float | None = None
    employees: int | None = None  # average number during the period
    filed_on: date | None = None  # when the accounts these figures came from were filed
    source_url: str | None = None


class Financials(Sourced):
    """Figures from filed accounts, newest period first."""

    periods: list[FinancialPeriod] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)  # e.g. accounts filed as PDF only


# --- share capital, charges, ownership, related companies, risk ----------------


class CapitalEvent(BaseModel):
    """A share capital filing. An allotment (SH01) means new shares were issued,
    which usually signals a funding round or share scheme."""

    kind: str  # allotment, cancellation, buyback, reduction, subdivision, other
    form: str | None = None  # SH01, SH06, ...
    event_date: date | None = None  # when it happened
    filed_on: date | None = None
    description: str | None = None
    share_capital: float | None = None  # total nominal share capital afterwards
    currency: str | None = None
    document_url: str | None = None


class Capital(Sourced):
    events: list[CapitalEvent] = Field(default_factory=list)  # newest first
    notes: list[str] = Field(default_factory=list)


class Charge(BaseModel):
    """A mortgage or charge: security given to a lender."""

    charge_number: int | None = None
    status: str | None = None  # outstanding, fully-satisfied, part-satisfied
    classification: str | None = None
    created_on: date | None = None
    delivered_on: date | None = None
    satisfied_on: date | None = None
    lenders: list[str] = Field(default_factory=list)  # persons entitled
    particulars: str | None = None
    secured: str | None = None


class Charges(Sourced):
    total: int = 0
    outstanding: int = 0
    satisfied: int = 0
    part_satisfied: int = 0
    items: list[Charge] = Field(default_factory=list)  # newest first, capped
    notes: list[str] = Field(default_factory=list)


class ParentCompany(BaseModel):
    """A corporate owner, from the register of persons with significant control."""

    level: int  # 1 = direct owner, 2 = the owner's owner, ...
    name: str
    company_number: str | None = None  # if registered at Companies House
    controls: str | None = None  # the company one level down that it controls
    natures_of_control: list[str] = Field(default_factory=list)
    legal_form: str | None = None
    country_registered: str | None = None
    status: str | None = None
    notified_on: date | None = None
    is_ultimate: bool = False  # top of the chain as far as the UK register shows


class Controller(BaseModel):
    """A person (or non-company body) at the top of an ownership chain, as
    recorded in the official PSC register."""

    name: str
    kind: str
    natures_of_control: list[str] = Field(default_factory=list)
    of_company: str | None = None
    of_company_number: str | None = None


class Ownership(Sourced):
    parents: list[ParentCompany] = Field(default_factory=list)  # ordered by level
    controllers: list[Controller] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class RelatedCompany(BaseModel):
    """Another company that shares one or more current officers with this one."""

    company_number: str
    name: str
    status: str | None = None
    shared_officers: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)


class RelatedCompanies(Sourced):
    companies: list[RelatedCompany] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class RiskFlag(BaseModel):
    code: str  # stable identifier, e.g. accounts_overdue
    severity: Literal["info", "warning", "high"]
    message: str
    evidence: str | None = None


class Risk(BaseModel):
    """Signals computed from registry data (no LLM). A flag is a prompt to look
    closer, not a verdict."""

    flags: list[RiskFlag] = Field(default_factory=list)


# --- website --------------------------------------------------------------


class WebPage(BaseModel):
    url: str
    kind: str  # home, about, products, investors, customers, careers, contact, news, other
    status_code: int | None = None
    title: str | None = None
    text_chars: int = 0  # how much readable text the page had
    rendered: bool = False  # loaded with a headless browser (JavaScript-built page)
    fetched_at: datetime | None = None  # when the page was really downloaded
    cached: bool = False  # this run reused a locally cached copy


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
    capital: Capital = Field(default_factory=Capital)
    charges: Charges = Field(default_factory=Charges)
    ownership: Ownership = Field(default_factory=Ownership)
    related_companies: RelatedCompanies = Field(default_factory=RelatedCompanies)
    risk: Risk = Field(default_factory=Risk)
    website: Website = Field(default_factory=Website)
    products: list[Product] = Field(default_factory=list)
    tech_stack: list[str] = Field(default_factory=list)
    news: list[NewsItem] = Field(default_factory=list)
    hiring: Hiring = Field(default_factory=Hiring)

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)
