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
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr

SCHEMA_VERSION = "0.16.0"

SourceName = Literal[
    "companies_house",
    "website",
    "news",
    "jobs",
    "llm_extraction",
    "gleif",
    "contracts_finder",
    "dns",
    "certificates",
    "wayback",
    "electoral_commission",
    "land_registry",
    "other",
]


def utcnow() -> datetime:
    return datetime.now(dt.UTC).replace(microsecond=0)


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
    total_assets: float | None = None
    employees: int | None = None  # average number during the period
    filed_on: date | None = None  # when the accounts these figures came from were filed
    source_url: str | None = None
    # How the figures were read: "ixbrl" (exact tagged figures), "annual_report" (the
    # company's own text PDF, read by the LLM) or "ocr" (a scanned filing, OCR then LLM;
    # occasional digit errors are possible).
    method: Literal["ixbrl", "annual_report", "ocr"] | None = None


class Financials(Sourced):
    """Figures from filed accounts, newest period first."""

    periods: list[FinancialPeriod] = Field(default_factory=list)
    latest_filing_format: Literal["ixbrl", "pdf"] | None = None  # how the newest accounts were filed
    principal_activities: str | None = None  # as stated in the accounts
    auditor: str | None = None
    ultimate_controlling_party: str | None = None  # as stated in the accounts
    document_url: str | None = None  # the document the PDF figures came from
    notes: list[str] = Field(default_factory=list)  # e.g. accounts filed as PDF only
    # (statement, document URL) about political donations, found while reading the
    # accounts; the pipeline moves it to political_donations. Not part of the output.
    _political_statement: tuple[str, str | None] | None = PrivateAttr(default=None)


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


class StructuredData(BaseModel):
    """Facts the site publishes about itself in machine-readable form (schema.org
    JSON-LD and OpenGraph tags). Exact, and read without an LLM."""

    name: str | None = None
    legal_name: str | None = None
    description: str | None = None
    logo_url: str | None = None
    founding_date: str | None = None
    employees: str | None = None
    vat_id: str | None = None
    parent_organisation: str | None = None
    schema_types: list[str] = Field(default_factory=list)  # e.g. Organization, Bakery


class Wikipedia(Sourced):
    """The company's Wikipedia article and Wikidata facts (organisation facts only;
    founders and other people listed there are not read)."""

    title: str | None = None
    url: str | None = None
    wikidata_id: str | None = None
    matched_by: Literal["company_number", "search"] | None = None  # how the entry was found
    description: str | None = None  # short description, e.g. "bakery chain in the United Kingdom"
    summary: str | None = None  # the article's lead section
    founded: str | None = None  # year
    employees: int | None = None
    employees_as_of: str | None = None  # year of the employee figure
    industries: list[str] = Field(default_factory=list)
    headquarters: str | None = None
    parent_organisation: str | None = None
    stock_listings: list[str] = Field(default_factory=list)  # e.g. "London Stock Exchange: GRG"
    official_websites: list[str] = Field(default_factory=list)
    types: list[str] = Field(default_factory=list)  # e.g. "public company", "bakery"
    references: list[Reference] = Field(default_factory=list)  # the article's cited sources
    notes: list[str] = Field(default_factory=list)


class Reference(BaseModel):
    """One source cited by the Wikipedia article: something to read further."""

    text: str  # the citation as shown, e.g. '"Greggs to open 150 shops". BBC News. 3 March 2024.'
    url: str | None = None  # the cited page, if it's online
    title: str | None = None  # the linked title, when there is one


class Milestone(Sourced):
    """A dated event in the company's history (LLM, checked against the source text)."""

    year: str
    event: str


class FundingRound(Sourced):
    year: str | None = None
    round: str | None = None  # seed, Series A, IPO, private placement...
    amount: str | None = None  # as written in the source, e.g. "£500 million"
    investors: list[str] = Field(default_factory=list)  # organisations


class Shareholder(Sourced):
    """An organisation reported to own shares (funds, companies, governments).
    Individual people are not recorded; registered controllers are in `ownership`."""

    name: str
    stake: str | None = None  # as written, e.g. "25%"


class Funding(BaseModel):
    rounds: list[FundingRound] = Field(default_factory=list)  # oldest first
    shareholders: list[Shareholder] = Field(default_factory=list)


class KeyFact(Sourced):
    text: str


class Customer(Sourced):
    """An organisation named as a customer (LLM, checked against the source text), and
    its Companies House match if it's a UK company."""

    name: str  # as the source names it
    evidence: str | None = None  # how it's mentioned: "case study", "client list"...
    company_number: str | None = None
    registered_name: str | None = None  # the name on the Companies House register
    status: str | None = None  # active, dissolved...
    match: Literal["exact", "likely", "none"] | None = None  # how sure the register match is
    researched: bool = False  # a profile of this customer was built in the same run
    profile_path: str | None = None  # where that profile was saved


class WebMention(BaseModel):
    """A page elsewhere on the web that mentions the company (from web search)."""

    title: str
    url: str
    snippet: str | None = None


class WebMentions(Sourced):
    query: str | None = None
    items: list[WebMention] = Field(default_factory=list)
    searches_used: int = 0  # real API calls this lookup (cached repeats are free)
    notes: list[str] = Field(default_factory=list)


class SitePost(BaseModel):
    """A post from the site's own WordPress feed (news, blog)."""

    title: str
    url: str | None = None
    published_on: date | None = None
    excerpt: str | None = None


class ShopCatalogue(BaseModel):
    """A summary of the site's Shopify product feed."""

    products_listed: int = 0  # products in the feed (the feed lists at most 250)
    product_types: dict[str, int] = Field(default_factory=dict)  # type -> number of products
    vendors: dict[str, int] = Field(default_factory=dict)  # brand -> number of products
    price_min: float | None = None  # in the shop's own currency
    price_max: float | None = None
    newest: list[str] = Field(default_factory=list)  # titles of the newest products


class Website(Sourced):
    url: str | None = None
    structured: StructuredData = Field(default_factory=StructuredData)
    discovered_via: Literal["user", "wikidata", "domain_guess", "search"] | None = None
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
    # Analytics and tag IDs in the pages (GTM-..., G-..., UA-..., meta-pixel:...). Sites
    # sharing an ID are usually run by the same organisation (see index.json).
    analytics_ids: list[str] = Field(default_factory=list)
    posts: list[SitePost] = Field(default_factory=list)  # newest first, from /wp-json
    shop: ShopCatalogue | None = None  # from /products.json, for Shopify sites
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
    summary: str | None = None  # the feed's short snippet, never the article text
    sentiment: float | None = None  # -1.0 (negative), 0.0 (neutral), 1.0 (positive); LLM
    topic: str | None = None  # kind of story, from a fixed list (LLM)
    provider: str | None = None  # which news source returned it: bing, gdelt, guardian
    # False when the LLM thinks the headline isn't really about this company (it only
    # shares the name, or mentions it in passing). Kept rather than dropped, because
    # small models misjudge some genuine stories; consumers can filter on it.
    relevant: bool | None = None


class News(Sourced):
    """Recent headlines that name the company, newest first. Items with
    relevant=False are doubtful (see NewsItem.relevant)."""

    items: list[NewsItem] = Field(default_factory=list)
    searched_for: list[str] = Field(default_factory=list)  # names used in the search
    window_days: int | None = None
    providers: list[str] = Field(default_factory=list)  # sources that answered
    notes: list[str] = Field(default_factory=list)


class JobPosting(Sourced):
    title: str
    department: str | None = None
    location: str | None = None
    url: str | None = None
    posted_at: date | None = None


class Hiring(Sourced):
    """Open roles from the company's public job board (Greenhouse, Lever, Workable,
    Ashby, SmartRecruiters or Recruitee). Titles, teams and places only: no people."""

    provider: str | None = None  # greenhouse, lever, workable, ashby, smartrecruiters, recruitee
    board_url: str | None = None
    found_via: Literal["website", "name"] | None = None  # linked from the site, or found by name
    total: int = 0  # open roles on the board (open_roles may be capped)
    departments: dict[str, int] = Field(default_factory=dict)  # department -> open roles
    locations: dict[str, int] = Field(default_factory=dict)  # location -> open roles
    open_roles: list[JobPosting] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# --- other registers and public records ---------------------------------------


class LeiEntity(BaseModel):
    lei: str
    name: str
    country: str | None = None  # of its legal address
    company_number: str | None = None  # when registered at Companies House


class LegalEntity(Sourced):
    """The company's Legal Entity Identifier (LEI) record from GLEIF, the global register
    of legal entities used in financial reporting. Lists organisations only: when the
    owners are people, GLEIF records only that fact (parent_exception NATURAL_PERSONS)."""

    lei: str | None = None
    matched_by: Literal["company_number", "name"] | None = None
    legal_name: str | None = None
    other_names: list[str] = Field(default_factory=list)  # trading or previous names
    entity_status: str | None = None  # ACTIVE or INACTIVE
    registration_status: str | None = None  # ISSUED, LAPSED (not renewed), RETIRED...
    first_registered: date | None = None
    next_renewal: date | None = None
    headquarters: str | None = None  # city, country
    direct_parent: LeiEntity | None = None
    ultimate_parent: LeiEntity | None = None
    parent_exception: str | None = None  # why no parent is reported, e.g. NON_CONSOLIDATING
    subsidiaries: list[LeiEntity] = Field(default_factory=list)  # direct children, capped
    subsidiaries_total: int = 0
    isins: list[str] = Field(default_factory=list)  # securities it has issued (capped)
    bic: list[str] = Field(default_factory=list)  # bank identifier codes
    notes: list[str] = Field(default_factory=list)


class PublicContract(Sourced):
    """A UK public-sector contract awarded to the company (Contracts Finder)."""

    title: str
    buyer: str | None = None  # the public body
    value: float | None = None  # awarded value in GBP
    awarded_on: date | None = None
    start: date | None = None
    end: date | None = None
    category: str | None = None  # CPV description, e.g. "IT services"
    region: str | None = None
    # Suppliers named on the award. Above 1 it's a framework or shared award, and the
    # value is the whole award's, not this company's share.
    suppliers: int = 1
    url: str | None = None


class PublicContracts(Sourced):
    searched_for: str | None = None
    found: int = 0  # awards naming the company (items may be capped)
    sole_supplier_value: float | None = None  # total of the awards it won alone
    top_buyers: dict[str, int] = Field(default_factory=dict)  # buyer -> awards
    items: list[PublicContract] = Field(default_factory=list)  # newest first
    notes: list[str] = Field(default_factory=list)


class PoliticalDonation(Sourced):
    """A donation the company made, from the Electoral Commission's register."""

    recipient: str  # the party, campaign group or politician (public figures) who received it
    recipient_type: str  # "Political Party", "MP - Member of Parliament", "Permitted Participant"...
    to_individual: bool = False  # the recipient is a person (an MP, candidate...)
    accounting_unit: str | None = None  # the part of the party, e.g. "Central Party" or a constituency
    value: float  # GBP
    accepted_on: date | None = None
    donation_type: str | None = None  # Cash, Non Cash...
    nature: str | None = None  # for non-cash donations, e.g. Travel, Sponsorship
    sponsorship: bool = False
    register_name: str | None = None  # the register: Great Britain or Northern Ireland
    reference: str  # the register's own reference, e.g. C0841869
    matched_by: Literal["company_number", "name"] = "company_number"
    url: str | None = None


class PoliticalDonations(Sourced):
    searched_for: list[str] = Field(default_factory=list)  # names searched (current, previous)
    found: int = 0
    total_value: float | None = None
    by_recipient: dict[str, float] = Field(default_factory=dict)  # recipient -> total GBP
    first_donation: date | None = None
    last_donation: date | None = None
    items: list[PoliticalDonation] = Field(default_factory=list)  # newest first, capped
    # Donations by other UK companies in the group (parents, subsidiaries), each matched on
    # its own company number; only those that donated are listed.
    group: list[GroupDonations] = Field(default_factory=list)
    group_searched: list[str] = Field(default_factory=list)  # "NAME (number, relation)"
    # What the latest accounts say about political donations (directors' report): companies
    # giving over 2,000 GBP a year must disclose it. Quoted, not interpreted.
    accounts_statement: str | None = None
    accounts_statement_url: str | None = None
    notes: list[str] = Field(default_factory=list)


class GroupDonations(Sourced):
    name: str
    company_number: str
    relation: Literal["parent", "subsidiary"]
    found: int = 0
    total_value: float | None = None
    by_recipient: dict[str, float] = Field(default_factory=dict)
    first_donation: date | None = None
    last_donation: date | None = None
    items: list[PoliticalDonation] = Field(default_factory=list)  # newest first, capped


class Domain(Sourced):
    """Signals from the website's domain: public DNS records (email provider, services the
    domain is verified with), certificate transparency logs (subdomains, other domains on
    the same certificates) and the Wayback Machine (how long the site has existed)."""

    name: str | None = None
    email_provider: str | None = None  # from the MX records, e.g. Microsoft 365
    email_services: list[str] = Field(default_factory=list)  # allowed to send its email (SPF)
    verified_services: list[str] = Field(default_factory=list)  # domain-verification TXT records
    email_security: list[str] = Field(default_factory=list)  # e.g. "DMARC policy: reject"
    subdomains: list[str] = Field(default_factory=list)
    other_domains: list[str] = Field(default_factory=list)  # on the same certificates
    first_archived: date | None = None  # oldest Wayback Machine snapshot
    last_archived: date | None = None
    notes: list[str] = Field(default_factory=list)


class CoOwner(BaseModel):
    name: str
    company_number: str | None = None  # Companies House number only (never a foreign one)
    category: str | None = None  # e.g. "Limited Company or Public Limited Company"
    country: str | None = None  # country of incorporation, for overseas companies


class PropertyTitle(BaseModel):
    """A registered title (land or building) in England and Wales owned by the company,
    from HM Land Registry's "UK companies that own property" dataset."""

    title_number: str
    tenure: str | None = None  # Freehold / Leasehold
    address: str | None = None
    district: str | None = None
    county: str | None = None
    region: str | None = None
    postcode: str | None = None
    multiple_addresses: bool = False  # the title covers more addresses than shown
    price_paid: float | None = None  # GBP, when the register records one
    owner_since: date | None = None  # "Date Proprietor Added"
    owner_address: str | None = None  # the company's address on the title (for notices)
    co_owners: list[CoOwner] = Field(default_factory=list)  # other organisations on the title
    matched_by: Literal["company_number", "name"] = "company_number"
    # "ccod" = UK companies dataset, "ocod" = overseas companies dataset
    dataset: Literal["ccod", "ocod"] = "ccod"


class GroupProperty(BaseModel):
    """Titles owned by another company in the group. UK companies are matched on their
    Companies House number; overseas ones (from the overseas companies dataset) on their
    exact name, with the country of incorporation checked when both records give one."""

    name: str
    company_number: str | None = None  # None for an overseas company not registered here
    relation: Literal["parent", "subsidiary"]
    dataset: Literal["ccod", "ocod"] = "ccod"
    country: str | None = None  # country of incorporation (overseas companies)
    matched_by: Literal["company_number", "name"] = "company_number"
    titles: int = 0
    freehold: int = 0
    leasehold: int = 0
    total_price_paid: float | None = None
    regions: dict[str, int] = Field(default_factory=dict)


class PropertyHoldings(Sourced):
    """Land and buildings in England and Wales registered to the company. The dataset covers
    titles owned by companies and other organisations only, never private individuals.
    Prices are what was paid when the company was registered as owner (often blank), not
    current values."""

    dataset: str | None = None  # file the local copy was built from, e.g. CCOD_FULL_2026_11.zip
    data_as_of: date | None = None  # when HM Land Registry published that data
    # the overseas companies dataset, when it was searched (overseas group companies, or
    # the company itself when it is an overseas company registered here: FC/OE numbers)
    overseas_dataset: str | None = None
    overseas_data_as_of: date | None = None
    found: int = 0
    freehold: int = 0
    leasehold: int = 0
    total_price_paid: float | None = None  # sum of the recorded prices
    priced_titles: int = 0  # how many titles have a recorded price
    regions: dict[str, int] = Field(default_factory=dict)  # region -> titles
    first_acquired: date | None = None
    last_acquired: date | None = None
    items: list[PropertyTitle] = Field(default_factory=list)  # newest first, capped
    group: list[GroupProperty] = Field(default_factory=list)  # only companies owning titles
    group_searched: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


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
    wikipedia: Wikipedia = Field(default_factory=Wikipedia)
    website: Website = Field(default_factory=Website)
    products: list[Product] = Field(default_factory=list)
    tech_stack: list[str] = Field(default_factory=list)
    news: News = Field(default_factory=News)
    history: list[Milestone] = Field(default_factory=list)  # oldest first
    key_facts: list[KeyFact] = Field(default_factory=list)
    funding: Funding = Field(default_factory=Funding)
    web_mentions: WebMentions = Field(default_factory=WebMentions)
    customers: list[Customer] = Field(default_factory=list)
    hiring: Hiring = Field(default_factory=Hiring)
    lei: LegalEntity = Field(default_factory=LegalEntity)
    public_contracts: PublicContracts = Field(default_factory=PublicContracts)
    political_donations: PoliticalDonations = Field(default_factory=PoliticalDonations)
    domain: Domain = Field(default_factory=Domain)
    property: PropertyHoldings = Field(default_factory=PropertyHoldings)

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)
