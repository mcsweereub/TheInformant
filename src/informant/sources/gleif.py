"""Legal Entity Identifier (LEI) records from GLEIF's free public API (no key).

An LEI is the global ID a company needs to trade securities or report to financial
regulators. Its record shows the legal name, other names, the parent companies as the
company itself reports them (often foreign parents the UK register doesn't show), its
subsidiaries with their own LEIs, and the securities it has issued (ISINs).

The record is found by company number (registeredAs + jurisdiction GB). Some records are
registered against another authority (banks, for instance, under their FCA number), so
when that finds nothing, an exact legal-name match in the UK is accepted instead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime

import httpx

from ..schema import LegalEntity, LeiEntity, Source
from .fetchutil import api_client, cached_get
from .website.extract import legal_key

API = "https://api.gleif.org/api/v1"
MAX_SUBSIDIARIES = 50
MAX_ISINS = 20


@dataclass
class GleifResult:
    section: LegalEntity


def _date(value: str | None) -> date | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date() if value else None
    except ValueError:
        return None


COMPANIES_HOUSE_RA = "RA000585"  # GLEIF's code for the Companies House register


def _entity(record: dict) -> LeiEntity:
    e = record["attributes"]["entity"]
    at_ch = (e.get("registeredAt") or {}).get("id") == COMPANIES_HOUSE_RA
    return LeiEntity(lei=record["id"], name=e["legalName"]["name"],
                     country=(e.get("legalAddress") or {}).get("country"),
                     company_number=(e.get("registeredAs") or None) if at_ch else None)


class Gleif:
    def __init__(self, cache=None, http: httpx.Client | None = None) -> None:
        self.cache = cache
        self.http = http or api_client(headers={"Accept": "application/vnd.api+json"})

    def get(self, url: str, params: dict | None = None) -> dict | None:
        """A JSON document, or None when GLEIF says there's nothing there (404)."""
        try:
            return json.loads(cached_get(self.http, self.cache, url, params))
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    def find(self, company_number: str, company_name: str) -> tuple[dict | None, str | None]:
        by_number = self.get(f"{API}/lei-records", {"filter[entity.registeredAs]": company_number,
                                                    "filter[entity.jurisdiction]": "GB"})
        records = (by_number or {}).get("data") or []
        if records:
            return records[0], "company_number"
        # GLEIF's name search is fuzzy ("MONZO BANK LIMITED" finds "MONZO BANK HOLDING
        # GROUP LIMITED" first), so search without the suffix and compare strictly.
        wanted = legal_key(company_name)
        by_name = self.get(f"{API}/lei-records", {"filter[fulltext]": wanted,
                                                  "filter[entity.legalAddress.country]": "GB",
                                                  "page[size]": 20})
        for record in (by_name or {}).get("data") or []:
            e = record["attributes"]["entity"]
            if legal_key(e["legalName"]["name"]) == wanted and e.get("jurisdiction") == "GB":
                return record, "name"
        return None, None

    def related(self, record: dict, name: str) -> dict | None:
        """A related resource (direct-parent, isins...). Single records list the relationship
        but not always its link, so the address is built here; 404 means there's none."""
        if name not in (record.get("relationships") or {}):
            return None
        return self.get(f"{API}/lei-records/{record['id']}/{name}")


def fetch_lei(company_number: str, company_name: str, *, cache=None,
              http: httpx.Client | None = None) -> GleifResult:
    """The company's LEI record (an empty section if it has none). Network problems
    become notes."""
    own = http is None
    gleif = Gleif(cache=cache, http=http)
    section = LegalEntity()
    try:
        record, matched_by = gleif.find(company_number, company_name)
        if record is None:
            section.notes.append("no LEI record found (most small companies don't have one)")
            return GleifResult(section)
        a = record["attributes"]
        e, reg = a["entity"], a["registration"]
        hq = e.get("headquartersAddress") or {}
        section.lei = record["id"]
        section.matched_by = matched_by
        section.legal_name = e["legalName"]["name"]
        section.other_names = [n["name"] for n in e.get("otherNames") or [] if n.get("name")]
        section.entity_status = e.get("status")
        section.registration_status = reg.get("status")
        section.first_registered = _date(reg.get("initialRegistrationDate"))
        section.next_renewal = _date(reg.get("nextRenewalDate"))
        section.headquarters = ", ".join(p for p in (hq.get("city"), hq.get("country")) if p) or None
        section.bic = list(a.get("bic") or [])
        section.sources = [Source(name="gleif", url=f"https://search.gleif.org/#/record/{record['id']}")]
        if reg.get("status") == "LAPSED":
            section.notes.append("the LEI has lapsed (not renewed), so its details may be out of date")

        for rel, attr in (("direct-parent", "direct_parent"), ("ultimate-parent", "ultimate_parent")):
            doc = gleif.related(record, rel)
            if doc and doc.get("data"):
                setattr(section, attr, _entity(doc["data"]))
        if section.direct_parent is None:
            # Why no parent is reported: e.g. owned by people (NATURAL_PERSONS), or the
            # parent doesn't consolidate it (NON_CONSOLIDATING).
            exc = gleif.get(f"{API}/lei-records/{record['id']}/direct-parent-reporting-exception")
            if exc and exc.get("data"):
                section.parent_exception = exc["data"]["attributes"].get("reason")
        children = gleif.related(record, "direct-children")
        if children:
            section.subsidiaries = [_entity(c) for c in children.get("data") or []][:MAX_SUBSIDIARIES]
            total = ((children.get("meta") or {}).get("pagination") or {}).get("total")
            section.subsidiaries_total = total or len(section.subsidiaries)
        isins = gleif.related(record, "isins")
        if isins:
            section.isins = [i["attributes"]["isin"] for i in isins.get("data") or []][:MAX_ISINS]
    except (httpx.HTTPError, ValueError, KeyError) as e:
        section.notes.append(f"GLEIF lookup failed: {type(e).__name__}")
    finally:
        if own:
            gleif.http.close()
    return GleifResult(section)
