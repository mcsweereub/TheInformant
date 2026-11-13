"""The outcome of an extraction, plus the checks and tidying applied to every
model's answer before it's trusted."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .prompts import (
    MERGE_SYSTEM_PROMPT,
    SECTORS,
    STAGE_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    WebsiteExtraction,
)


@dataclass
class ExtractionResult:
    data: WebsiteExtraction | None
    model: str | None = None
    notes: list[str] = field(default_factory=list)
    strategy: str | None = None  # "single" or "staged"


_ALL_PROMPTS = " ".join(p.lower() for p in (SYSTEM_PROMPT, STAGE_SYSTEM_PROMPT, MERGE_SYSTEM_PROMPT))
_PLACEHOLDERS = {"product name", "short category", "one-sentence description",
                 "office name", "city or town", "country"}


def echoed(value: str) -> bool:
    """True if `value` is prompt wording or a template placeholder, not a fact
    (small local models sometimes fill fields with the instructions)."""
    v = value.strip().lower().rstrip(".")
    return v in _PLACEHOLDERS or (len(v) > 25 and v in _ALL_PROMPTS)


def copied_instructions(data: WebsiteExtraction) -> bool:
    """True if the model echoed prompt wording instead of extracting facts."""
    if echoed(data.summary) or echoed(data.business_model):
        return True
    values = [p.name for p in data.products] + [p.description for p in data.products]
    values += data.target_markets + [o.label for o in data.offices]
    return bool(values) and sum(echoed(v) for v in values) >= max(1, len(values) // 3)


def clean(data: WebsiteExtraction) -> WebsiteExtraction:
    data.sectors = list(dict.fromkeys(s for s in data.sectors if s in SECTORS)) or ["Other"]
    # Small models often write snake_case ("uk_residents"); use plain words.
    data.tags = sorted({t.replace("_", " ").strip().lower() for t in data.tags if t.strip()})
    data.target_markets = list(dict.fromkeys(
        m.replace("_", " ").strip() for m in data.target_markets if m.strip()))
    return data


# --- grounding: is each item actually supported by the text the model was given? ---

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"and", "the", "for", "with", "from", "our", "your", "ltd", "limited", "plc", "inc"}
# Tags that classify a company rather than quote its pages, so they needn't appear in the text.
_GENERIC_TAGS = {
    "b2b", "b2c", "b2b2c", "d2c", "saas", "paas", "fintech", "edtech", "healthtech", "proptech",
    "ecommerce", "e-commerce", "marketplace", "retail", "wholesale", "franchise", "subscription",
    "nonprofit", "non-profit", "startup", "sme", "enterprise",
}


class Grounding:
    """Checks whether words appear in the source text, allowing for simple word
    endings (bakery / bakeries / bakes)."""

    def __init__(self, text: str) -> None:
        self.tokens = set(_WORD.findall(text.lower()))
        self._seen: dict[str, bool] = {}

    def has(self, word: str) -> bool:
        if word not in self._seen:
            if word in self.tokens:
                found = True
            elif len(word) < 5:
                found = False
            else:
                stem = word[:max(4, len(word) - 2)]
                found = any(t.startswith(stem) for t in self.tokens)
            self._seen[word] = found
        return self._seen[word]

    def share(self, phrase: str) -> float:
        """Fraction of the phrase's meaningful words found in the text (1.0 if none to check)."""
        words = [w for w in _WORD.findall(phrase.lower()) if len(w) >= 3 and w not in _STOP]
        if not words:
            return 1.0
        return sum(self.has(w) for w in words) / len(words)


MIN_TAGS = 3  # fewer than this after checking: top up from the text itself
_ACRONYM = re.compile(r"\b[A-Z]{2,6}\b")
_ALPHA = re.compile(r"[a-z]+")
_URL = re.compile(r"(?:https?://|www[.])[^ ]+")
_COMMON_ACRONYMS = {"UK", "US", "USA", "EU", "FAQ", "FAQS", "CEO", "CFO", "CTO", "PDF", "GDPR",
                    "VAT", "LTD", "PLC", "LLP", "INC", "GBP", "USD", "EUR", "AM", "PM", "TV", "ID",
                    "OK", "IT", "HR", "PR", "AI", "API", "URL", "HTTP", "HTTPS", "WWW", "RSS"}
_FILLER = {
    "the", "and", "for", "with", "from", "our", "your", "you", "are", "was", "were", "that",
    "this", "these", "those", "have", "has", "had", "will", "can", "all", "any", "but", "not",
    "more", "most", "about", "into", "over", "also", "their", "they", "them", "its", "who",
    "what", "when", "where", "how", "why", "which", "been", "being", "than", "then", "there",
    "here", "out", "use", "used", "using", "get", "one", "two", "new", "now", "may", "see",
    "page", "home", "menu", "contact", "read", "learn", "click", "find", "view", "back", "next",
    "cookie", "cookies", "privacy", "policy", "terms", "rights", "reserved", "copyright",
    "follow", "share", "search", "login", "sign", "email", "phone", "website", "site", "please",
    "select", "company", "limited", "ltd", "plc", "registered", "england", "wales", "number",
    "work", "team", "us", "we", "to", "of", "in", "on", "at", "by", "is", "it", "as", "an", "or",
    "be", "a", "do", "if", "so", "up", "no", "my", "me", "he", "she", "his", "her", "case",
    "studies", "study", "customers", "customer", "solutions", "solution", "services", "service",
}


def frequent_terms(text: str, limit: int, exclude: set[str],
                   company_name: str | None = None) -> list[str]:
    """Distinctive terms the text itself repeats: acronyms (LNG, ETRM) and two-word
    phrases (energy trading). Used to top up tags, so every one is grounded."""
    counts: dict[str, int] = {}
    for line in text.splitlines():
        if line.startswith(("=== ", "Title:")):
            continue  # our own page headers, not the site's wording
        line = _URL.sub(" ", line)
        for acronym in _ACRONYM.findall(line):
            if acronym not in _COMMON_ACRONYMS:
                counts[acronym.lower()] = counts.get(acronym.lower(), 0) + 1
        words = _ALPHA.findall(line.lower())
        for a, b in zip(words, words[1:]):
            if len(a) >= 4 and len(b) >= 4 and a not in _FILLER and b not in _FILLER and a != b:
                counts[f"{a} {b}"] = counts.get(f"{a} {b}", 0) + 1
    # The company's own name is frequent on its site but says nothing about what it does.
    name_words = set(_ALPHA.findall((company_name or "").lower()))
    ranked = sorted((t for t, n in counts.items()
                     if n >= 3 and t not in exclude and not set(t.split()) <= name_words),
                    key=lambda t: (-counts[t], t))
    return ranked[:limit]


def _norm(s: str) -> str:
    return " ".join(_WORD.findall(s.lower()))


def merge_duplicate_offices(offices: list) -> list:
    """'London' and 'London office' are one site: per locality, keep the entries
    that carry a real address, or just the first if none does."""
    groups: dict[str, list] = {}
    for o in offices:
        key = _norm(o.locality) or _norm(o.label).replace("office", "").strip() or _norm(o.label)
        groups.setdefault(key, []).append(o)
    kept = []
    for key, group in groups.items():
        detailed = [o for o in group if _norm(o.address) and _norm(o.address) != key]
        unique: dict[str, object] = {}
        for o in detailed:
            unique.setdefault(_norm(o.address), o)
        kept.extend(unique.values() if unique else group[:1])
    return kept


def ground(data: WebsiteExtraction, source_text: str, notes: list[str],
           company_name: str | None = None) -> WebsiteExtraction:
    """Drop tags, products and sites that the source text doesn't support.

    Small models sometimes invent or distort items (a site about LNG tagged "lpg").
    A product or site is kept when at least half of its words appear in the text;
    a tag must be fully supported, unless it's a generic classification like "b2b".
    """
    g = Grounding(source_text)

    kept_tags = [t for t in data.tags if t in _GENERIC_TAGS or g.share(t) == 1.0]
    dropped = [t for t in data.tags if t not in kept_tags]
    if dropped:
        notes.append("dropped tag(s) not supported by the page text: " + ", ".join(dropped))
    if len(kept_tags) < MIN_TAGS:
        added = frequent_terms(source_text, limit=5 - len(kept_tags), exclude=set(kept_tags),
                               company_name=company_name)
        if added:
            notes.append("added tag(s) from terms the pages repeat: " + ", ".join(added))
            kept_tags = sorted(kept_tags + added)
    data.tags = kept_tags

    kept_products = [p for p in data.products if g.share(p.name) >= 0.5]
    if len(kept_products) < len(data.products):
        gone = [p.name for p in data.products if p not in kept_products]
        notes.append("dropped product(s) not found in the page text: " + ", ".join(gone))
    data.products = kept_products

    kept_offices = [o for o in data.offices
                    if g.share(f"{o.locality} {o.address}".strip() or o.label) >= 0.5]
    if len(kept_offices) < len(data.offices):
        gone = [o.label or o.locality for o in data.offices if o not in kept_offices]
        notes.append("dropped site(s) not found in the page text: " + ", ".join(gone))
    data.offices = merge_duplicate_offices(kept_offices)
    return data


def finish(data: WebsiteExtraction, model: str | None, notes: list[str],
           source_text: str | None = None, company_name: str | None = None) -> ExtractionResult:
    """Reject echoed prompts, tidy the rest, and (given the text the model saw)
    drop items that text doesn't support."""
    if copied_instructions(data):
        return ExtractionResult(None, model=model, notes=notes + [
            f"{model} copied the prompt instead of extracting facts; result discarded "
            "(try a larger model)"])
    data = clean(data)
    if source_text:
        data = ground(data, source_text, notes, company_name)
    return ExtractionResult(data, model=model, notes=notes)
