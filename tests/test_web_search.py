"""Web search (Tavily): budget, caching, failure handling, mentions, and its use to
find a missing website or annual report. A fake Tavily; no real searches."""

import json

import httpx
import pytest
import respx

from informant.cache import PageCache
from informant.pipeline import research_web_mentions
from informant.schema import CompanyProfile, Identity
from informant.sources.web_search import TAVILY_URL, WebSearch
from informant.sources.website.discovery import discover_website
from informant.sources.website.fetch import Crawler


def tavily(results):
    return {"query": "q", "results": [
        {"title": t, "url": u, "content": c, "score": 0.5} for t, u, c in results]}


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("INFORMANT_SEARCH", "on")
    monkeypatch.delenv("INFORMANT_SEARCH_MAX", raising=False)


def test_from_env_needs_key_and_respects_switch(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert WebSearch.from_env() is None
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    monkeypatch.setenv("INFORMANT_SEARCH", "off")
    assert WebSearch.from_env() is None
    monkeypatch.setenv("INFORMANT_SEARCH", "on")
    monkeypatch.setenv("INFORMANT_SEARCH_MAX", "2")
    assert WebSearch.from_env().max_searches == 2


@respx.mock
def test_search_sends_key_caches_and_keeps_to_budget(key, tmp_path):
    route = respx.post(TAVILY_URL).respond(json=tavily([("A", "https://a.example/", "snippet")]))
    s = WebSearch.from_env(cache=PageCache(tmp_path))
    s.max_searches = 2
    assert [r.url for r in s.search("acme")] == ["https://a.example/"]
    assert route.calls[0].request.headers["authorization"] == "Bearer tvly-test"
    assert json.loads(route.calls[0].request.content)["search_depth"] == "basic"

    s.search("acme")  # cached: free
    assert route.call_count == 1 and s.used == 1
    s.search("second query")
    assert s.search("third query") == [] and route.call_count == 2  # budget of 2 reached
    assert any("budget reached" in n for n in s.notes)

    fresh = WebSearch.from_env(cache=PageCache(tmp_path))
    fresh.search("acme")  # a later lookup reuses the cached result
    assert route.call_count == 2 and fresh.used == 0


@respx.mock
def test_search_failures_are_notes(key):
    s = WebSearch.from_env()
    respx.post(TAVILY_URL).respond(401)
    assert s.search("x") == [] and "rejected the API key" in s.notes[-1]
    respx.post(TAVILY_URL).respond(432)
    assert s.search("y") == [] and "allowance" in s.notes[-1]
    respx.post(TAVILY_URL).mock(side_effect=httpx.ConnectError("offline"))
    assert s.search("z") == [] and "failed" in s.notes[-1]


@respx.mock
def test_mentions_filter_people_social_and_unrelated(key):
    route = respx.post(TAVILY_URL).respond(json=tavily([
        ("Planlogic wins energy software award", "https://trade.example/award", "Planlogic's EOS ETRM..."),
        ("Jane Smith - Planlogic | LinkedIn", "https://uk.linkedin.com/in/jane", "Director at Planlogic"),
        ("Best LNG trading tools", "https://other.example/list", "A list with no company name"),
    ]))
    profile = CompanyProfile(query="x", identity=Identity(
        name="PLANLOGIC SOFTWARE LIMITED", website="https://planlogic.com"))
    material = research_web_mentions(profile, WebSearch.from_env())

    assert [m.url for m in profile.web_mentions.items] == ["https://trade.example/award"]
    body = json.loads(route.calls[0].request.content)
    assert "linkedin.com" in body["exclude_domains"] and "planlogic.com" in body["exclude_domains"]
    assert body["query"] == '"planlogic" company'
    assert material.kind == "web_search" and "wins energy software award" in material.text


@respx.mock
def test_search_finds_a_website_guessing_missed(key):
    # Every guessed domain (zxqwidgets.co.uk, zxq-widgets.com...) doesn't exist.
    respx.get(url__regex=r"https://zxq-?widgets\.[a-z.]+/.*").mock(side_effect=httpx.ConnectError("no dns"))
    respx.post(TAVILY_URL).respond(json=tavily([
        ("ZXQ Widgets on Endole", "https://suite.endole.co.uk/insight/company/01234567", ""),
        ("ZXQ Widgets | Industrial widgets", "https://www.zxq-industrial.example/about", ""),
    ]))
    respx.get("https://www.zxq-industrial.example/robots.txt").respond(404)
    respx.get("https://www.zxq-industrial.example/").respond(html=(
        "<html><head><title>ZXQ Widgets</title></head><body>"
        "ZXQ Widgets Limited, registered in England no. 01234567</body></html>"))
    with Crawler(delay=0) as c:
        found = discover_website(c, company_name="ZXQ WIDGETS LIMITED", company_number="01234567",
                                 postcode=None, search=WebSearch.from_env())
    assert found.url.rstrip("/") == "https://www.zxq-industrial.example"
    assert found.via == "search" and found.confidence == "high"
    assert "found by web search" in found.reasons
