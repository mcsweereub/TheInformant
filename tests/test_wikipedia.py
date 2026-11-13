"""Wikipedia and Wikidata: lookup by company number, search fallback with checks,
facts, article material, and how it feeds the rest of the profile."""

import json

import httpx
import pytest
import respx

from informant.pipeline import research_wikipedia
from informant.schema import CompanyProfile, Identity
from informant.sources.website.research import summary_sentences
from informant.sources.wikipedia import (
    SUMMARY_API,
    WIKIDATA_API,
    WIKIPEDIA_API,
    fetch_wikipedia,
)


def claim(value, qualifiers=None):
    if isinstance(value, str) and value.startswith("Q"):
        dv = {"value": {"id": value}}
    elif isinstance(value, dict):
        dv = {"value": value}
    else:
        dv = {"value": value}
    c = {"mainsnak": {"datavalue": dv}}
    if qualifiers:
        c["qualifiers"] = qualifiers
    return c


GREGGS = {
    "labels": {"en": {"value": "Greggs"}},
    "descriptions": {"en": {"value": "bakery chain in the United Kingdom"}},
    "sitelinks": {"enwiki": {"title": "Greggs"}},
    "claims": {
        "P2622": [claim("00502851")],
        "P17": [claim("Q145")],
        "P31": [claim("Q891723")],
        "P452": [claim("Q540912")],
        "P571": [claim({"time": "+1939-01-01T00:00:00Z"})],
        "P159": [claim("Q1425428")],
        "P414": [claim("Q171240", {"P249": [{"datavalue": {"value": "GRG"}}]})],
        "P856": [claim("https://www.greggs.co.uk/")],
        "P1128": [claim({"amount": "+30000"}, {"P585": [{"datavalue": {"value": {"time": "+2023-01-01T00:00:00Z"}}}]}),
                  claim({"amount": "+32000"}, {"P585": [{"datavalue": {"value": {"time": "+2025-01-01T00:00:00Z"}}}]})],
        "P112": [claim("Q999")],  # founder: a person, must not be read
    },
}
LABELS = {"Q891723": "public company", "Q540912": "food industry", "Q1425428": "Newcastle upon Tyne",
          "Q171240": "London Stock Exchange", "Q999": "Jane Founder"}
ARTICLE = """Greggs plc is a British bakery chain.

== History ==
Founded in 1939 in Newcastle.

== Senior leadership ==
Jane Founder is the chief executive.

=== List of former chairmen ===
Someone Else (1990-2000)

== Products ==
Sausage rolls and steak bakes.

=== Animal welfare ===
Cage-free eggs since 2020.

== References ==
1. Some citation"""


def wikidata_router(entities: dict, search_hits: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.params
        if p.get("action") == "query":
            number = p["srsearch"].split("=")[-1]
            hits = [{"title": q} for q in search_hits.get(number, [])]
            return httpx.Response(200, json={"query": {"search": hits}})
        ids = p["ids"].split("|")
        if p["props"] == "labels":
            return httpx.Response(200, json={"entities": {
                i: {"labels": {"en": {"value": LABELS.get(i, i)}}} for i in ids}})
        return httpx.Response(200, json={"entities": {i: entities[i] for i in ids if i in entities}})
    return handler


@pytest.fixture
def contact(monkeypatch):
    monkeypatch.setenv("INFORMANT_CONTACT", "someone@example.com")


REFERENCES_HTML = """<html><body><p>Article</p>
<ol class="mw-references references">
 <li id="cite_note-1"><span class="mw-reference-text reference-text"><a rel="mw:ExtLink" class="external text"
   href="https://www.bbc.co.uk/news/greggs-150-shops">"Greggs to open 150 shops"</a>. BBC News. 3 March 2024.</span></li>
 <li id="cite_note-2"><span class="reference-text">Smith, A. (1990). <i>The Greggs Story</i>. A book with no link.</span></li>
 <li id="cite_note-3"><span class="reference-text"><a class="external" href="//www.ft.com/content/x">FT piece</a></span></li>
 <li id="cite_note-4"><span class="reference-text"><a class="external text" href="https://www.bbc.co.uk/news/greggs-150-shops">"Greggs to open 150 shops"</a> (repeat)</span></li>
</ol></body></html>"""


def mock_greggs_article():
    respx.get(url__startswith="https://en.wikipedia.org/api/rest_v1/page/html/").respond(
        html=REFERENCES_HTML)
    respx.get(url__startswith=SUMMARY_API).respond(json={
        "title": "Greggs", "description": "Bakery chain in the United Kingdom",
        "extract": "Greggs plc is a British bakery and fast food chain. It specialises in savoury "
                   "products. It was founded in Newcastle."})
    return respx.get(WIKIPEDIA_API, params={"prop": "extracts"}).respond(json={
        "query": {"pages": {"1": {"extract": ARTICLE}}}})


def test_skipped_without_contact_details(monkeypatch):
    monkeypatch.delenv("INFORMANT_CONTACT", raising=False)
    result = fetch_wikipedia("GREGGS PLC", "00502851")
    assert result.material is None and "INFORMANT_CONTACT" in result.section.notes[0]


@respx.mock
def test_exact_match_by_company_number(contact):
    wd = respx.get(WIKIDATA_API).mock(side_effect=wikidata_router({"Q3403981": GREGGS},
                                                                  {"00502851": ["Q3403981"]}))
    mock_greggs_article()
    r = fetch_wikipedia("GREGGS PLC", "00502851")
    s = r.section

    assert s.matched_by == "company_number" and s.wikidata_id == "Q3403981"
    assert s.title == "Greggs" and s.url == "https://en.wikipedia.org/wiki/Greggs"
    assert s.description == "bakery chain in the United Kingdom" and s.founded == "1939"
    assert s.industries == ["food industry"] and s.headquarters == "Newcastle upon Tyne"
    assert s.stock_listings == ["London Stock Exchange: GRG"] and s.types == ["public company"]
    assert (s.employees, s.employees_as_of) == (32000, "2025")  # the latest figure
    assert s.summary.startswith("Greggs plc is a British bakery")
    assert "Jane Founder" not in json.dumps(s.model_dump(mode="json"))  # people are not read
    assert "someone@example.com" in wd.calls[0].request.headers["user-agent"]  # Wikimedia policy

    text = r.material.text
    assert r.material.kind == "wikipedia" and text.startswith("Description: bakery chain")
    assert "History:\nFounded in 1939" in text and "Products:" in text
    assert "References" not in text and "Some citation" not in text
    assert "Senior leadership" not in text and "Jane Founder" not in text  # people sections dropped
    assert "Animal welfare:" in text  # sections after the people ones are kept

    refs = s.references
    assert [(x.url, x.title) for x in refs] == [
        ("https://www.bbc.co.uk/news/greggs-150-shops", '"Greggs to open 150 shops"'),
        (None, None),  # a book: citation text only
        ("https://www.ft.com/content/x", "FT piece"),  # protocol-relative link completed
    ]  # the repeated citation appears once
    assert refs[1].text.startswith("Smith, A. (1990). The Greggs Story")


@respx.mock
def test_search_fallback_accepts_only_a_matching_uk_company(contact):
    other_uk = {"labels": {"en": {"value": "Acme Widgets"}}, "claims": {
        "P17": [claim("Q145")], "P2622": [claim("09999999")]}}  # different company number
    american = {"labels": {"en": {"value": "Acme Widgets"}}, "claims": {"P17": [claim("Q30")]}}
    right = {"labels": {"en": {"value": "Acme Widgets"}}, "descriptions": {"en": {"value": "UK maker"}},
             "claims": {"P17": [claim("Q145")]}, "sitelinks": {}}
    respx.get(WIKIDATA_API).mock(side_effect=wikidata_router(
        {"Q1": other_uk, "Q2": american, "Q3": right}, {}))
    respx.get(WIKIPEDIA_API, params={"list": "search"}).respond(json={"query": {"search": [
        {"title": "Acme Widgets (US)"}, {"title": "Acme Widgets plc"}, {"title": "Acme Widgets"}]}})
    respx.get(WIKIPEDIA_API, params={"prop": "pageprops"}).respond(json={"query": {"pages": {
        "1": {"title": "Acme Widgets (US)", "pageprops": {"wikibase_item": "Q2"}},
        "2": {"title": "Acme Widgets plc", "pageprops": {"wikibase_item": "Q1"}},
        "3": {"title": "Acme Widgets", "pageprops": {"wikibase_item": "Q3"}}}}})

    r = fetch_wikipedia("ACME WIDGETS LIMITED", "01234567")
    assert r.section.wikidata_id == "Q3" and r.section.matched_by == "search"
    assert any("no English Wikipedia article" in n for n in r.section.notes)
    assert r.material.text == "Description: UK maker"


@respx.mock
def test_not_found_and_network_failure_are_notes(contact):
    respx.get(WIKIDATA_API).mock(side_effect=wikidata_router({}, {}))
    respx.get(WIKIPEDIA_API, params={"list": "search"}).respond(json={"query": {"search": []}})
    r = fetch_wikipedia("PLANLOGIC SOFTWARE LIMITED", "09461711")
    assert r.material is None and r.section.notes == ["no Wikipedia or Wikidata entry found"]

    respx.get(WIKIDATA_API).mock(side_effect=httpx.ConnectError("offline"))
    r = fetch_wikipedia("GREGGS PLC", "00502851")
    assert "Wikipedia lookup failed" in r.section.notes[0]


@respx.mock
def test_research_wikipedia_adds_industry_tags(contact):
    respx.get(WIKIDATA_API).mock(side_effect=wikidata_router({"Q3403981": GREGGS},
                                                             {"00502851": ["Q3403981"]}))
    mock_greggs_article()
    profile = CompanyProfile(query="x", identity=Identity(name="GREGGS PLC", company_number="00502851"))
    profile.classification.tags = ["bakery"]
    events = []
    material = research_wikipedia(profile, progress=events.append)
    assert material is not None and profile.wikipedia.title == "Greggs"
    assert profile.classification.tags == ["bakery", "food industry"]
    assert [e.status for e in events] == ["started", "done"]


def test_summary_sentences():
    text = "Greggs plc is a British bakery chain. It sells sausage rolls. It was founded in 1939."
    assert summary_sentences(text) == "Greggs plc is a British bakery chain. It sells sausage rolls."
    assert summary_sentences(None) is None
