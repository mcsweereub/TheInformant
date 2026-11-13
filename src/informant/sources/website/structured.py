"""Facts a website publishes about itself in machine-readable form.

Many sites embed schema.org data (JSON-LD) and OpenGraph tags for search engines
and social networks: official name, description, logo, founding date, address,
phone, social accounts. Reading these is exact and instant, needs no LLM, and
gives the LLM a reliable head start.

Only organisation-level facts are read. People listed in the markup (founders,
employees) are ignored.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup

from .parse import Page

_ORG_TYPES = {
    "organization", "corporation", "localbusiness", "ngo", "onlinebusiness", "onlinestore",
    "educationalorganization", "governmentorganization", "medicalorganization", "airline",
    "financialservice", "bankorcreditunion", "insuranceagency", "professionalservice",
    "store", "restaurant", "bakery", "foodestablishment", "hotel", "lodgingbusiness",
    "automotivebusiness", "homeandconstructionbusiness", "legalservice", "realestateagent",
    "travelagency", "newsmediaorganization", "sportsorganization", "project", "brand",
}


@dataclass
class StructuredFacts:
    name: str | None = None
    legal_name: str | None = None
    description: str | None = None
    logo_url: str | None = None
    founding_date: str | None = None
    employees: str | None = None
    vat_id: str | None = None
    parent_organisation: str | None = None
    schema_types: list[str] = field(default_factory=list)
    same_as: list[str] = field(default_factory=list)
    telephones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    addresses: list[dict[str, str | None]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any((self.name, self.legal_name, self.description, self.logo_url,
                        self.founding_date, self.employees, self.same_as, self.telephones,
                        self.emails, self.addresses))

    def as_text(self) -> str:
        """A short fact sheet to hand to the LLM alongside the page text."""
        rows = [
            ("Name", self.name), ("Legal name", self.legal_name),
            ("Description", self.description), ("Founded", self.founding_date),
            ("Employees", self.employees), ("Parent organisation", self.parent_organisation),
            ("Type", ", ".join(self.schema_types) or None),
        ]
        lines = [f"{label}: {value}" for label, value in rows if value]
        for a in self.addresses:
            parts = [a.get(k) for k in ("street", "locality", "region", "postal_code", "country")]
            lines.append("Address: " + ", ".join(p for p in parts if p))
        return "\n".join(lines)


def _text(value: Any) -> str | None:
    """schema.org values may be a string, a number, an object or a list of those."""
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("name") or value.get("url") or value.get("@value") or value.get("value")
    if value is None or isinstance(value, (dict, list)):
        return None
    text = " ".join(str(value).split())
    return text or None


def _types(obj: dict) -> list[str]:
    t = obj.get("@type")
    return [str(x) for x in (t if isinstance(t, list) else [t]) if x]


def _is_organisation(obj: dict) -> bool:
    types = [t.lower() for t in _types(obj)]
    return any(t in _ORG_TYPES or t.endswith(("organization", "business", "store")) for t in types) \
        or "legalName" in obj


def _walk(node: Any):
    """Yield every JSON object in a JSON-LD block, including @graph members and nesting."""
    if isinstance(node, list):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, dict):
        yield node
        for key, value in node.items():
            if key == "@graph" or isinstance(value, (dict, list)):
                yield from _walk(value)


def _json_ld_objects(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    objects: list[dict] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        try:
            data = json.loads(raw, strict=False)
        except (ValueError, TypeError):
            continue  # sites often publish broken JSON-LD; skip that block
        objects.extend(_walk(data))
    return objects


def _address(value: Any) -> dict[str, str | None] | None:
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, str):
        return {"street": " ".join(value.split()), "locality": None, "region": None,
                "postal_code": None, "country": None}
    if not isinstance(value, dict):
        return None
    address = {
        "street": _text(value.get("streetAddress")),
        "locality": _text(value.get("addressLocality")),
        "region": _text(value.get("addressRegion")),
        "postal_code": _text(value.get("postalCode")),
        "country": _text(value.get("addressCountry")),
    }
    return address if any(address.values()) else None


def _employees(value: Any) -> str | None:
    if isinstance(value, dict):
        low, high = value.get("minValue"), value.get("maxValue")
        if low is not None and high is not None:
            return f"{low}-{high}"
        return _text(value.get("value") or low or high)
    return _text(value)


def _add(target: list, value: Any) -> None:
    for item in (value if isinstance(value, list) else [value]):
        text = _text(item)
        if text and text not in target:
            target.append(text)


def _meta(soup: BeautifulSoup, *names: str) -> str | None:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content", "").strip():
            return " ".join(tag["content"].split())
    return None


def extract_structured(pages: list[Page]) -> StructuredFacts:
    """Merge organisation facts from every page; earlier pages (the homepage) win."""
    facts = StructuredFacts()

    def first(attr: str, value: str | None) -> None:
        if value and getattr(facts, attr) is None:
            setattr(facts, attr, value)

    for page in pages:
        if not page.html:
            continue
        for obj in _json_ld_objects(page.html):
            types = [t.lower() for t in _types(obj)]
            if "website" in types:
                first("name", _text(obj.get("name")))
                continue
            if not _is_organisation(obj):
                continue
            for t in _types(obj):
                if t not in facts.schema_types:
                    facts.schema_types.append(t)
            first("name", _text(obj.get("name")))
            first("legal_name", _text(obj.get("legalName")))
            first("description", _text(obj.get("description")))
            first("logo_url", _text(obj.get("logo")))
            first("founding_date", _text(obj.get("foundingDate")))
            first("employees", _employees(obj.get("numberOfEmployees")))
            first("vat_id", _text(obj.get("vatID")))
            first("parent_organisation", _text(obj.get("parentOrganization")))
            _add(facts.same_as, obj.get("sameAs") or [])
            _add(facts.telephones, obj.get("telephone") or [])
            _add(facts.emails, obj.get("email") or [])
            contacts = obj.get("contactPoint") or []
            for cp in contacts if isinstance(contacts, list) else [contacts]:
                if isinstance(cp, dict):
                    _add(facts.telephones, cp.get("telephone") or [])
                    _add(facts.emails, cp.get("email") or [])
            addresses = obj.get("address") or []
            for a in addresses if isinstance(addresses, list) else [addresses]:
                parsed = _address(a)
                if parsed and parsed not in facts.addresses:
                    facts.addresses.append(parsed)

    # OpenGraph / meta tags fill gaps, from the homepage only.
    home = next((p for p in pages if p.html), None)
    if home is not None:
        soup = BeautifulSoup(home.html, "html.parser")
        first("name", _meta(soup, "og:site_name"))
        first("description", _meta(soup, "og:description", "description"))
        first("logo_url", _meta(soup, "og:image"))
    return facts
