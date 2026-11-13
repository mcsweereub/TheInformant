"""The extraction prompt, the fixed sector list, and the output schema the
model must fill. Shared by every backend so Claude and local models behave alike."""

from __future__ import annotations

from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, BeforeValidator, Field

# A fixed vocabulary keeps categorisation consistent across companies. It is part
# of the output schema itself (an enum), so a model that honours the schema can't
# write anything else.
Sector = Literal[
    "Agriculture & Food Production", "Automotive", "Construction & Real Estate",
    "Consumer Goods", "Creative & Media", "Defence & Aerospace", "Education",
    "Energy & Utilities", "Engineering & Manufacturing", "Financial Services",
    "Food & Drink Retail", "Government & Public Sector", "Healthcare & Life Sciences",
    "Hospitality & Leisure", "Insurance", "Legal & Professional Services",
    "Logistics & Transport", "Marketing & Advertising", "Mining & Materials",
    "Non-profit & Charity", "Retail & E-commerce", "Software & IT Services",
    "Telecommunications", "Travel & Tourism", "Other",
]
SECTORS: list[str] = list(get_args(Sector))

BusinessModel = Literal["B2B", "B2C", "B2B2C", "marketplace", "public sector", "mixed", "unknown"]
BUSINESS_MODELS: tuple[str, ...] = get_args(BusinessModel)
BUSINESS_MODEL_GUIDE = (
    "B2C if it sells mainly to individual consumers, B2B if it sells mainly to other "
    "businesses, B2B2C if it reaches consumers through other businesses, marketplace if it "
    "connects buyers and sellers, public sector if it mainly serves government bodies, "
    "mixed if it clearly serves both consumers and businesses, and unknown if the text "
    "doesn't say. A shop, bank or app that the public uses directly is B2C, or mixed if "
    "it also has business customers; it is B2B2C only when its products reach the public "
    "through partner companies")

MAX_SECTORS, MAX_TAGS, MAX_MARKETS, MAX_ITEMS, MAX_FACTS = 3, 10, 8, 15, 12


# The schema states the limits (so constrained decoding enforces them), and these
# validators make parsing tolerant when a backend ignores them: unknown values are
# dropped and long lists cut, instead of the whole answer being rejected.
def _known_sectors(value: Any) -> Any:
    if isinstance(value, list):
        return [s for s in value if s in SECTORS][:MAX_SECTORS]
    return value


def _known_business_model(value: Any) -> Any:
    if isinstance(value, str):
        wanted = value.strip().lower()
        return next((m for m in BUSINESS_MODELS if m.lower() == wanted), "unknown")
    return value


def _at_most(n: int) -> BeforeValidator:
    return BeforeValidator(lambda v: v[:n] if isinstance(v, list) else v)


SectorList = Annotated[list[Sector], BeforeValidator(_known_sectors), Field(max_length=MAX_SECTORS)]
BusinessModelField = Annotated[BusinessModel, BeforeValidator(_known_business_model)]
TagList = Annotated[list[str], _at_most(MAX_TAGS), Field(max_length=MAX_TAGS)]
MarketList = Annotated[list[str], _at_most(MAX_MARKETS), Field(max_length=MAX_MARKETS)]
FactList = Annotated[list[str], _at_most(MAX_FACTS), Field(max_length=MAX_FACTS)]
MAX_MILESTONES, MAX_FUNDING, MAX_SHAREHOLDERS = 20, 15, 15

# Shared by the single-call and per-part prompts.
KEY_FACTS_GUIDE = (
    "Give up to 12 key facts: short, self-contained sentences about what the company does, who "
    "it sells to, where it operates, how big it is, its partners and its notable achievements. "
    "Each sentence names the company instead of saying \"they\".")
HISTORY_AND_FUNDING_GUIDE = """List up to 20 milestones from the company's history: dated events such as its founding, launches, entering new markets, acquisitions, mergers, stock market listings, rebrands, closures and legal or regulatory events. Give the year as four digits and the event as one short sentence. Leave out events about individual people.
List the funding rounds the text describes (investment or share sales), each with the year, the kind of round (for example seed, Series A, IPO, private placement), the amount as written, and the names of the investing organisations.
List notable shareholders: organisations (investment funds, companies, banks, governments) that the text says own shares in the company, with the stake as written if given. Leave out individual people."""

SYSTEM_PROMPT = f"""You read the text of a company's own website and record facts about that company for a personal research database. You reply with JSON only.

Work only from the website text you are given. When the pages don't state something, use an empty list, or the word unknown for a single value. Don't add facts from general knowledge.

Record facts about the company, never about people: leave out the names and details of individual employees, directors, founders and private customers. Don't name any individual person in any value; write "the founders" or "the chief executive" instead.

What to record:
Write a summary of two or three plain sentences saying what the company does and who its customers are.
Choose between one and three sectors, using only these exact names: {"; ".join(SECTORS)}.
Give between three and ten tags: short lowercase keywords that would help group similar companies, such as bakery, franchise, saas or b2b payments. Take each tag from wording the text itself uses, spelled the same way, and make every tag different in meaning from the others.
Give the business model: {BUSINESS_MODEL_GUIDE}.
List the target markets: the kinds of customers or the regions the company sells to.
List up to 15 of the main products, services or product lines, each with a one-sentence description and a short category.
List up to 15 physical sites the company operates now that the pages name, such as offices, factories or flagship locations. For a chain with hundreds of shops, give only the headquarters and mention the shop network in the summary. Places that matter only to its history (a first shop, a former factory) belong in the milestones instead.
{KEY_FACTS_GUIDE}
{HISTORY_AND_FUNDING_GUIDE}
For each product, site, milestone, funding round and shareholder, set source_page to the web address shown on the PAGE line of the page where you found it.

Every value you write must describe this particular company and come from its pages. Never copy wording from these instructions into your answer."""


class ExtractedMilestone(BaseModel):
    year: str  # four digits
    event: str
    source_page: str


class ExtractedFunding(BaseModel):
    year: str
    round: str  # seed, Series A, IPO, private placement...
    amount: str  # as written, e.g. "£500 million"
    investors: list[str]  # organisations
    source_page: str


class ExtractedShareholder(BaseModel):
    name: str  # an organisation
    stake: str  # as written, e.g. "about 25%", or empty
    source_page: str


class ExtractedProduct(BaseModel):
    name: str
    description: str
    category: str
    source_page: str  # URL of the page it came from (checked against the crawled pages)


class ExtractedOffice(BaseModel):
    label: str  # e.g. "Head office", "Manchester office"
    address: str
    locality: str
    country: str
    source_page: str


ProductList = Annotated[list[ExtractedProduct], _at_most(MAX_ITEMS), Field(max_length=MAX_ITEMS)]
OfficeList = Annotated[list[ExtractedOffice], _at_most(MAX_ITEMS), Field(max_length=MAX_ITEMS)]
MilestoneList = Annotated[list[ExtractedMilestone], _at_most(MAX_MILESTONES),
                          Field(max_length=MAX_MILESTONES)]
FundingList = Annotated[list[ExtractedFunding], _at_most(MAX_FUNDING), Field(max_length=MAX_FUNDING)]
ShareholderList = Annotated[list[ExtractedShareholder], _at_most(MAX_SHAREHOLDERS),
                            Field(max_length=MAX_SHAREHOLDERS)]


class WebsiteExtraction(BaseModel):
    summary: str
    business_model: BusinessModelField
    sectors: SectorList
    tags: TagList
    target_markets: MarketList
    products: ProductList
    offices: OfficeList
    key_facts: Annotated[list[str], _at_most(20), Field(max_length=20)]
    milestones: MilestoneList
    funding_rounds: FundingList
    shareholders: ShareholderList


# --- staged extraction: notes per part, then one merge ------------------------
#
# For local models, material is read in parts that each fit comfortably in the
# model's context. Each part yields notes (ChunkFindings); code combines products
# and sites; a final call turns the combined notes into the profile (MergedProfile).

STAGE_SYSTEM_PROMPT = """You read one part of the material gathered about a company and write notes about that company for a personal research database. You reply with JSON only.

Work only from the text you are given. Don't add facts from general knowledge. If this part says nothing useful about the company, return empty lists.

Record facts about the company, never about people: leave out the names and details of individual employees, directors, founders and private customers. Don't name any individual person in any value; write "the founders" or "the chief executive" instead.

What to record:
""" + KEY_FACTS_GUIDE + """
Products: up to 15 of the products, services or product lines this part mentions, each with a one-sentence description and a short category.
Sites: up to 15 physical sites the company operates now that this part names, such as offices, factories or flagship locations. Places that matter only to its history belong in the milestones instead.
Target markets: the kinds of customers or the regions the company sells to.
""" + HISTORY_AND_FUNDING_GUIDE + """
For each product, site, milestone, funding round and shareholder, set source_page to the web address shown on the PAGE line of the page where you found it.

Every value you write must describe this particular company and come from this text. Never copy wording from these instructions into your answer."""


class ChunkFindings(BaseModel):
    key_facts: FactList
    products: ProductList
    offices: OfficeList
    target_markets: MarketList
    milestones: MilestoneList
    funding_rounds: FundingList
    shareholders: ShareholderList


MERGE_SYSTEM_PROMPT = f"""You combine research notes about a company into a short profile for a personal research database. You reply with JSON only.

Use only the notes you are given. Don't add facts from general knowledge. Describe the company, never individual people.

What to write:
Write a summary of two or three plain sentences saying what the company does and who its customers are.
Choose between one and three sectors, using only these exact names: {"; ".join(SECTORS)}.
Give between three and ten tags: short lowercase keywords that would help group similar companies, such as bakery, franchise, saas or b2b payments. Take each tag from wording the text itself uses, spelled the same way, and make every tag different in meaning from the others.
Give the business model: {BUSINESS_MODEL_GUIDE}.
List the target markets, combining the ones in the notes without repeating any.

Every value you write must describe this particular company and come from the notes. Never copy wording from these instructions into your answer."""


class MergedProfile(BaseModel):
    summary: str
    business_model: BusinessModelField
    sectors: SectorList
    tags: TagList
    target_markets: MarketList


def stage_message(company_name: str, text: str, part: int, parts: int) -> str:
    return (
        f"Company: {company_name}\n\nMaterial (part {part} of {parts}):\n\n{text}\n\n"
        f"=== END OF MATERIAL\n\nNow write the JSON notes for {company_name}, "
        "using only facts from the material above."
    )


def merge_message(company_name: str, facts: list[str], products: list[str],
                  markets: list[str]) -> str:
    lines = "\n".join(f"- {f}" for f in facts) or "- (no facts found)"
    return (
        f"Company: {company_name}\n\nNotes:\n{lines}\n\n"
        f"Products and services: {'; '.join(products) or 'none noted'}\n"
        f"Target markets noted: {'; '.join(markets) or 'none noted'}\n\n"
        f"=== END OF NOTES\n\nNow write the JSON profile for {company_name}, "
        "using only the notes above."
    )


def user_message(company_name: str, page_text: str) -> str:
    # The closing instruction comes after the pages: small local models follow
    # what they read last most closely.
    return (
        f"Company: {company_name}\n\nWebsite pages:\n\n{page_text}\n\n"
        f"=== END OF PAGES\n\nNow write the JSON record for {company_name}, "
        "using only facts from the pages above."
    )
