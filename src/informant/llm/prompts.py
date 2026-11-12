"""The extraction prompt, the fixed sector list, and the output schema the
model must fill. Shared by every backend so Claude and local models behave alike."""

from __future__ import annotations

from pydantic import BaseModel

# A fixed vocabulary keeps categorisation consistent across companies.
SECTORS = [
    "Agriculture & Food Production", "Automotive", "Construction & Real Estate",
    "Consumer Goods", "Creative & Media", "Defence & Aerospace", "Education",
    "Energy & Utilities", "Engineering & Manufacturing", "Financial Services",
    "Food & Drink Retail", "Government & Public Sector", "Healthcare & Life Sciences",
    "Hospitality & Leisure", "Insurance", "Legal & Professional Services",
    "Logistics & Transport", "Marketing & Advertising", "Mining & Materials",
    "Non-profit & Charity", "Retail & E-commerce", "Software & IT Services",
    "Telecommunications", "Travel & Tourism", "Other",
]

SYSTEM_PROMPT = f"""You read the text of a company's own website and record facts about that company for a personal research database. You reply with JSON only.

Work only from the website text you are given. When the pages don't state something, use an empty list, or the word unknown for a single value. Don't add facts from general knowledge.

Record facts about the company, never about people: leave out the names and details of individual employees, directors and private customers.

What to record:
Write a summary of two or three plain sentences saying what the company does and who its customers are.
Choose between one and three sectors, using only these exact names: {"; ".join(SECTORS)}.
Give between three and ten tags: short lowercase keywords that would help group similar companies, such as bakery, franchise, saas or b2b payments.
Give the business model as exactly one of: B2B, B2C, B2B2C, marketplace, public sector, mixed, unknown.
List the target markets: the kinds of customers or the regions the company sells to.
List up to 15 of the main products, services or product lines, each with a one-sentence description and a short category.
List up to 15 physical sites that the pages name, such as offices, factories or flagship locations. For a chain with hundreds of shops, give only the headquarters and mention the shop network in the summary.
For each product and each site, set source_page to the web address shown on the PAGE line of the page where you found it.

Every value you write must describe this particular company and come from its pages. Never copy wording from these instructions into your answer."""


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


class WebsiteExtraction(BaseModel):
    summary: str
    business_model: str
    sectors: list[str]
    tags: list[str]
    target_markets: list[str]
    products: list[ExtractedProduct]
    offices: list[ExtractedOffice]


# --- staged extraction: notes per part, then one merge ------------------------
#
# For local models, material is read in parts that each fit comfortably in the
# model's context. Each part yields notes (ChunkFindings); code combines products
# and sites; a final call turns the combined notes into the profile (MergedProfile).

STAGE_SYSTEM_PROMPT = """You read one part of the material gathered about a company and write notes about that company for a personal research database. You reply with JSON only.

Work only from the text you are given. Don't add facts from general knowledge. If this part says nothing useful about the company, return empty lists.

Record facts about the company, never about people: leave out the names and details of individual employees, directors and private customers.

What to record:
Key facts: up to 12 short, self-contained sentences about what the company does, who it sells to, where it operates, how big it is, its history, its partners and its notable achievements. Each sentence names the company instead of saying "they".
Products: up to 15 of the products, services or product lines this part mentions, each with a one-sentence description and a short category.
Sites: up to 15 physical sites this part names, such as offices, factories or flagship locations.
Target markets: the kinds of customers or the regions the company sells to.
For each product and each site, set source_page to the web address shown on the PAGE line of the page where you found it.

Every value you write must describe this particular company and come from this text. Never copy wording from these instructions into your answer."""


class ChunkFindings(BaseModel):
    key_facts: list[str]
    products: list[ExtractedProduct]
    offices: list[ExtractedOffice]
    target_markets: list[str]


MERGE_SYSTEM_PROMPT = f"""You combine research notes about a company into a short profile for a personal research database. You reply with JSON only.

Use only the notes you are given. Don't add facts from general knowledge. Describe the company, never individual people.

What to write:
Write a summary of two or three plain sentences saying what the company does and who its customers are.
Choose between one and three sectors, using only these exact names: {"; ".join(SECTORS)}.
Give between three and ten tags: short lowercase keywords that would help group similar companies, such as bakery, franchise, saas or b2b payments.
Give the business model as exactly one of: B2B, B2C, B2B2C, marketplace, public sector, mixed, unknown.
List the target markets, combining the ones in the notes without repeating any.

Every value you write must describe this particular company and come from the notes. Never copy wording from these instructions into your answer."""


class MergedProfile(BaseModel):
    summary: str
    business_model: str
    sectors: list[str]
    tags: list[str]
    target_markets: list[str]


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
