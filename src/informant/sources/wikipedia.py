"""Wikipedia and Wikidata: an encyclopaedic description plus structured facts.

Finding the right article:
  1. Wikidata items can record a Companies House number (property P2622). A match on
     the number is exact, so it's tried first.
  2. Otherwise Wikipedia is searched by name, and a candidate is accepted only if its
     Wikidata item is a UK entity whose name matches, and doesn't record a different
     Companies House number.

From Wikidata: founding year, industries, headquarters, parent organisation, stock
listings and tickers, employee count, official websites. Founders and other people
are deliberately not read. From Wikipedia: the short description and the article's
lead, plus the opening sections, which are handed to the LLM as extra material.

Wikimedia requires contact details in the User-Agent, so this runs only when
INFORMANT_CONTACT is set (see discovery.wikidata_user_agent).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

from ..schema import Reference, Source, Wikipedia
from .fetchutil import cached_get
from .website.discovery import wikidata_user_agent
from .website.extract import core_name
from .website.parse import Page

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
SUMMARY_API = "https://en.wikipedia.org/api/rest_v1/page/summary/"
ARTICLE_HTML_API = "https://en.wikipedia.org/api/rest_v1/page/html/"
ARTICLE_URL = "https://en.wikipedia.org/wiki/"
UK = "Q145"
# A safety limit only: long articles are normal, and the staged LLM step shares space
# fairly between sources. Octopus Energy's article is ~18,000 characters.
MAX_ARTICLE_CHARS = 40_000
MAX_REFERENCES = 300
# Sections that are references and navigation, not description.
_END_SECTIONS = re.compile(
    r"^==+\s*(See also|References|Notes|Citations|External links|Further reading|Bibliography|"
    r"Sources|Footnotes)\s*==+\s*$", re.I | re.M)
_HEADING = re.compile(r"^(==+)\s*(.*?)\s*\1\s*$", re.M)
# Sections about people (boards, executives, founders' biographies): not recorded, and
# they'd take space from the company's own story. Dropped with their sub-sections.
_PEOPLE_SECTIONS = re.compile(
    r"\b(leadership|key people|people|board|directors?|management|executives?|chairm[ae]n|"
    r"chief executives?|founders?|personnel|staff|notable (employees|alumni)|governance)\b", re.I)


@dataclass
class WikiResult:
    section: Wikipedia
    material: Page | None = None  # article text for the LLM
    notes: list[str] = field(default_factory=list)


def _client(ua: str) -> httpx.Client:
    return httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": ua})


def _json(http: httpx.Client, cache, url: str, params: dict) -> dict:
    return json.loads(cached_get(http, cache, url, {**params, "format": "json"}))


# --- finding the item -------------------------------------------------------------


def _by_company_number(http, cache, number: str) -> str | None:
    data = _json(http, cache, WIKIDATA_API, {
        "action": "query", "list": "search", "srsearch": f"haswbstatement:P2622={number}"})
    hits = (data.get("query") or {}).get("search") or []
    return hits[0]["title"] if hits else None


def _entities(http, cache, ids: list[str], props: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 50):
        data = _json(http, cache, WIKIDATA_API, {
            "action": "wbgetentities", "ids": "|".join(ids[i:i + 50]), "props": props,
            "languages": "en", "sitefilter": "enwiki"})
        out.update(data.get("entities") or {})
    return out


def _claim_ids(entity: dict, prop: str) -> list[str]:
    ids = []
    for c in (entity.get("claims") or {}).get(prop, []):
        value = (c.get("mainsnak") or {}).get("datavalue", {}).get("value")
        if isinstance(value, dict) and value.get("id"):
            ids.append(value["id"])
    return ids


def _claim_strings(entity: dict, prop: str) -> list[str]:
    values = []
    for c in (entity.get("claims") or {}).get(prop, []):
        value = (c.get("mainsnak") or {}).get("datavalue", {}).get("value")
        if isinstance(value, str):
            values.append(value)
    return values


def _by_search(http, cache, company_name: str, number: str | None) -> str | None:
    core = core_name(company_name)
    if not core:
        return None
    data = _json(http, cache, WIKIPEDIA_API, {
        "action": "query", "list": "search", "srsearch": f'"{core}" company', "srlimit": 5})
    titles = [h["title"] for h in (data.get("query") or {}).get("search") or []]
    if not titles:
        return None
    props = _json(http, cache, WIKIPEDIA_API, {
        "action": "query", "prop": "pageprops", "ppprop": "wikibase_item", "titles": "|".join(titles)})
    by_title = {p.get("title"): (p.get("pageprops") or {}).get("wikibase_item")
                for p in ((props.get("query") or {}).get("pages") or {}).values()}
    candidates = [by_title.get(t) for t in titles if by_title.get(t)]
    if not candidates:
        return None
    entities = _entities(http, cache, candidates, "claims|labels")
    for qid in candidates:
        e = entities.get(qid) or {}
        numbers = _claim_strings(e, "P2622")
        if numbers and number not in numbers:
            continue  # a different registered company
        label = ((e.get("labels") or {}).get("en") or {}).get("value", "")
        if UK not in _claim_ids(e, "P17") or not label:
            continue
        name = core_name(label)
        if name == core or name.startswith(core + " ") or core.startswith(name + " "):
            return qid
    return None


# --- reading the item ------------------------------------------------------------------


def _year(entity: dict, prop: str) -> str | None:
    for c in (entity.get("claims") or {}).get(prop, []):
        value = (c.get("mainsnak") or {}).get("datavalue", {}).get("value")
        if isinstance(value, dict) and value.get("time"):
            return value["time"].lstrip("+")[:4]
    return None


def _latest_quantity(entity: dict, prop: str) -> tuple[int | None, str | None]:
    """The most recent value of a quantity with a 'point in time' qualifier."""
    best: tuple[str, int] | None = None
    for c in (entity.get("claims") or {}).get(prop, []):
        value = (c.get("mainsnak") or {}).get("datavalue", {}).get("value")
        if not isinstance(value, dict) or "amount" not in value:
            continue
        when = ""
        for q in (c.get("qualifiers") or {}).get("P585", []):
            when = (q.get("datavalue", {}).get("value") or {}).get("time", "").lstrip("+")[:10]
        try:
            amount = int(float(value["amount"]))
        except ValueError:
            continue
        if best is None or when > best[0]:
            best = (when, amount)
    if best is None:
        return None, None
    return best[1], (best[0][:4] or None)


def _listings(entity: dict, labels: dict[str, str]) -> list[str]:
    out = []
    for c in (entity.get("claims") or {}).get("P414", []):
        value = (c.get("mainsnak") or {}).get("datavalue", {}).get("value")
        if not isinstance(value, dict) or not value.get("id"):
            continue
        tickers = [q.get("datavalue", {}).get("value") for q in (c.get("qualifiers") or {}).get("P249", [])]
        exchange = labels.get(value["id"], value["id"])
        ticker = next((t for t in tickers if isinstance(t, str)), None)
        out.append(f"{exchange}: {ticker}" if ticker else exchange)
    return out


def _article_text(http, cache, title: str) -> str:
    data = _json(http, cache, WIKIPEDIA_API, {
        "action": "query", "prop": "extracts", "explaintext": 1, "exsectionformat": "wiki",
        "redirects": 1, "titles": title})
    pages = (data.get("query") or {}).get("pages") or {}
    text = next(iter(pages.values()), {}).get("extract") or ""
    end = _END_SECTIONS.search(text)
    if end:
        text = text[:end.start()]
    text = drop_people_sections(text)
    text = _HEADING.sub(lambda m: f"\n{m.group(2)}:", text)  # "== History ==" -> "History:"
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:MAX_ARTICLE_CHARS]


def drop_people_sections(text: str) -> str:
    """Remove sections about people, and everything under them, until the next
    heading at the same or a higher level."""
    out: list[str] = []
    pos = 0
    skip_level: int | None = None
    for m in _HEADING.finditer(text):
        level = len(m.group(1))
        if skip_level is None:
            out.append(text[pos:m.start()])
        elif level <= skip_level:
            skip_level = None
            out.append("")
        if skip_level is None and _PEOPLE_SECTIONS.search(m.group(2)):
            skip_level = level
        elif skip_level is None:
            out.append(m.group(0))
        pos = m.end()
    if skip_level is None:
        out.append(text[pos:])
    return "".join(out)


def fetch_references(http, cache, title: str) -> list[Reference]:
    """The article's numbered references: citation text and the cited link."""
    html = cached_get(http, cache, ARTICLE_HTML_API + quote(title.replace(" ", "_"), safe=""))
    soup = BeautifulSoup(html, "html.parser")
    refs: list[Reference] = []
    seen: set[str] = set()
    for item in soup.select("ol.references > li, ol.mw-references > li"):
        body = item.select_one(".reference-text") or item
        text = " ".join(body.get_text(" ", strip=True).split())
        link = body.select_one("a.external[href], a[rel~='mw:ExtLink'][href]")
        url = link["href"] if link is not None else None
        if url and url.startswith("//"):
            url = "https:" + url
        key = url or text
        if not text or key in seen:
            continue
        seen.add(key)
        refs.append(Reference(text=text[:500], url=url,
                              title=link.get_text(" ", strip=True) if link is not None else None))
        if len(refs) >= MAX_REFERENCES:
            break
    return refs


def fetch_wikipedia(company_name: str, company_number: str | None, *, cache=None,
                    http: httpx.Client | None = None) -> WikiResult:
    """Never raises for network or data problems: they become notes."""
    ua = wikidata_user_agent()
    if ua is None:
        return WikiResult(Wikipedia(notes=["skipped: Wikimedia needs contact details in the "
                                           "User-Agent; set INFORMANT_CONTACT to enable"]))
    own = http is None
    http = http or _client(ua)
    try:
        return _fetch(http, cache, company_name, company_number)
    except (httpx.HTTPError, ValueError, KeyError) as e:
        return WikiResult(Wikipedia(notes=[f"Wikipedia lookup failed ({type(e).__name__})"]))
    finally:
        if own:
            http.close()


def _fetch(http, cache, company_name: str, number: str | None) -> WikiResult:
    qid = _by_company_number(http, cache, number) if number else None
    matched_by = "company_number" if qid else None
    if qid is None:
        qid = _by_search(http, cache, company_name, number)
        matched_by = "search" if qid else None
    if qid is None:
        return WikiResult(Wikipedia(notes=["no Wikipedia or Wikidata entry found"]))

    entity = _entities(http, cache, [qid], "claims|sitelinks|descriptions|labels")[qid]
    wanted = {p: _claim_ids(entity, p) for p in ("P452", "P159", "P749", "P414", "P31")}
    label_ids = sorted({i for ids in wanted.values() for i in ids})
    labels = {k: ((v.get("labels") or {}).get("en") or {}).get("value", k)
              for k, v in (_entities(http, cache, label_ids, "labels") if label_ids else {}).items()}
    employees, employees_as_of = _latest_quantity(entity, "P1128")

    section = Wikipedia(
        wikidata_id=qid,
        matched_by=matched_by,
        description=((entity.get("descriptions") or {}).get("en") or {}).get("value"),
        founded=_year(entity, "P571"),
        employees=employees,
        employees_as_of=employees_as_of,
        industries=[labels.get(i, i) for i in wanted["P452"]],
        headquarters=next((labels.get(i, i) for i in wanted["P159"]), None),
        parent_organisation=next((labels.get(i, i) for i in wanted["P749"]), None),
        stock_listings=_listings(entity, labels),
        official_websites=_claim_strings(entity, "P856"),
        types=[labels.get(i, i) for i in wanted["P31"]],
        sources=[Source(name="other", url=f"https://www.wikidata.org/wiki/{qid}")],
    )

    title = ((entity.get("sitelinks") or {}).get("enwiki") or {}).get("title")
    if not title:
        section.notes.append("on Wikidata, but there is no English Wikipedia article")
        return WikiResult(section, _material(section, ""))
    section.title = title
    section.url = ARTICLE_URL + quote(title.replace(" ", "_"))
    summary = json.loads(cached_get(http, cache, SUMMARY_API + quote(title.replace(" ", "_"), safe="")))
    section.summary = summary.get("extract") or None
    section.description = section.description or summary.get("description")
    section.sources.append(Source(name="other", url=section.url))
    article = _article_text(http, cache, title)
    try:
        section.references = fetch_references(http, cache, title)
    except httpx.HTTPError as e:
        section.notes.append(f"references could not be read ({type(e).__name__})")
    return WikiResult(section, _material(section, article))


def _material(section: Wikipedia, article: str) -> Page | None:
    """The facts and article as one page of material for the LLM."""
    facts = [
        ("Description", section.description), ("Founded", section.founded),
        ("Industries", ", ".join(section.industries) or None),
        ("Headquarters", section.headquarters), ("Parent organisation", section.parent_organisation),
        ("Stock listings", ", ".join(section.stock_listings) or None),
        ("Employees", f"{section.employees:,}" + (f" ({section.employees_as_of})"
                                                  if section.employees_as_of else "")
         if section.employees else None),
    ]
    lines = [f"{k}: {v}" for k, v in facts if v]
    text = "\n".join(lines) + ("\n\n" + article if article else "")
    if not text.strip():
        return None
    return Page(url=section.url or f"https://www.wikidata.org/wiki/{section.wikidata_id}",
                kind="wikipedia", status_code=200,
                title=f"Wikipedia: {section.title or section.wikidata_id}", text=text.strip())
