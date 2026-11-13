"""News: providers, name matching, de-duplication, LLM judging, pipeline wiring."""

import json
from datetime import timedelta

import httpx
import pytest
import respx

from informant.pipeline import research_news
from informant.schema import CompanyProfile, Identity, utcnow
from informant.sources.news import (
    BING_URL,
    GDELT_URL,
    GUARDIAN_URL,
    fetch_news,
    mentions,
    search_names,
)

OLLAMA = "http://localhost:11434/api/chat"


def rss_date(days_ago: float) -> str:
    return (utcnow() - timedelta(days=days_ago)).strftime("%a, %d %b %Y %H:%M:%S GMT")


def gdelt_date(days_ago: float) -> str:
    return (utcnow() - timedelta(days=days_ago)).strftime("%Y%m%dT%H%M%SZ")


def bing_item(title, url, days_ago=1, source="BBC", desc="Snippet."):
    wrapped = ("http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;url="
               + url.replace(":", "%3a").replace("/", "%2f") + "&amp;mkt=en-gb")
    return (f"<item><title>{title}</title><link>{wrapped}</link><description>{desc}</description>"
            f"<pubDate>{rss_date(days_ago)}</pubDate><News:Source>{source}</News:Source></item>")


def bing_feed(*items):
    return ('<?xml version="1.0"?><rss xmlns:News="https://www.bing.com/news/search?q=x&amp;format=rss">'
            f"<channel>{''.join(items)}</channel></rss>")


BING = bing_feed(
    bing_item("Monzo in talks over takeover by Nubank", "https://news.example/monzo-nubank", 2),
    bing_item("Monzo&#39;s profits double as customers pass 12m", "https://news.example/profits", 5),
    bing_item("Best savings accounts this week", "https://news.example/savings", 1),  # no name
    bing_item("Monzo was fined years ago, says old report", "https://news.example/old", 200),  # too old
)
GDELT = {"articles": [
    {"url": "https://other.example/nubank", "title": "Monzo in talks over takeover by Nubank",
     "seendate": gdelt_date(2), "domain": "other.example"},  # same story as Bing: de-duplicated
    {"url": "https://cityam.example/ipo", "title": "Monzo bank weighs a London listing",
     "seendate": gdelt_date(10), "domain": "cityam.example"},
]}


@pytest.fixture
def feeds():
    respx.get("https://www.bing.com/robots.txt").respond(text="User-agent: *\nAllow: /news/search\n")
    return {"bing": respx.get(BING_URL).respond(text=BING),
            "gdelt": respx.get(GDELT_URL).respond(json=GDELT)}


def test_search_names_use_brand_and_registered_name():
    assert search_names("MONZO BANK LIMITED", "https://monzo.com") == ["monzo", "monzo bank"]
    assert search_names("GREGGS PLC", "https://www.greggs.com/") == ["greggs"]
    assert search_names("THE EXAMPLE GROUP HOLDINGS LTD", None) == ["example"]


def test_headline_must_name_the_company():
    assert mentions("Greggs's new hotel suite opens", ["greggs"])
    assert mentions("MONZO BANK posts record year", ["monzo bank"])
    assert not mentions("Greggory Smith wins award", ["greggs"])
    # Everyday-word brands need a capitalised match.
    assert mentions("Next raises profit forecast", ["next"])
    assert not mentions("What happens next for mortgages", ["next"])


@respx.mock
def test_providers_merge_filter_and_dedupe(feeds):
    result = fetch_news("MONZO BANK LIMITED", "https://monzo.com")
    titles = [a.title for a in result.articles]
    assert titles == ["Monzo in talks over takeover by Nubank",
                      "Monzo's profits double as customers pass 12m",
                      "Monzo bank weighs a London listing"]  # newest first
    first = result.articles[0]
    assert first.url == "https://news.example/monzo-nubank"  # Bing's redirect unwrapped
    assert first.provider == "bing" and first.summary == "Snippet."  # the richer copy is kept
    assert result.providers == ["bing", "gdelt"]
    assert any("doesn't name the company" in n for n in result.notes)
    queries = [c.request.url.params["q"] for c in feeds["bing"].calls]
    assert queries == ['"monzo"', '"monzo bank"']  # one per name: the feed ignores OR


@respx.mock
def test_a_failing_provider_is_skipped(feeds):
    respx.get(GDELT_URL).respond(500)
    result = fetch_news("MONZO BANK LIMITED", "https://monzo.com")
    assert result.providers == ["bing"] and len(result.articles) == 2
    assert any("gdelt news search failed" in n for n in result.notes)


@respx.mock
def test_gdelt_rate_limit_is_retried_once(feeds, monkeypatch):
    monkeypatch.setattr("informant.sources.news.time.sleep", lambda s: None)
    respx.get(GDELT_URL).mock(side_effect=[httpx.Response(429), httpx.Response(200, json=GDELT)])
    result = fetch_news("MONZO BANK LIMITED", "https://monzo.com")
    assert "gdelt" in result.providers


@respx.mock
def test_bing_skipped_if_robots_forbids_it(feeds):
    respx.get("https://www.bing.com/robots.txt").respond(text="User-agent: *\nDisallow: /news/\n")
    from informant.sources.website.fetch import Crawler

    with Crawler() as c:
        result = fetch_news("MONZO BANK LIMITED", "https://monzo.com", allowed=c.allowed)
    assert feeds["bing"].call_count == 0 and result.providers == ["gdelt"]


@respx.mock
def test_guardian_used_only_with_a_key(feeds, monkeypatch):
    guardian = respx.get(GUARDIAN_URL).respond(json={"response": {"results": [
        {"webTitle": "Monzo chief executive on the bank's next decade", "webUrl": "https://g.example/a",
         "webPublicationDate": (utcnow() - timedelta(days=3)).isoformat().replace("+00:00", "Z"),
         "fields": {"trailText": "<strong>Interview</strong> with the boss"}}]}})
    fetch_news("MONZO BANK LIMITED", "https://monzo.com")
    assert guardian.call_count == 0

    monkeypatch.setenv("GUARDIAN_API_KEY", "test-key")
    result = fetch_news("MONZO BANK LIMITED", "https://monzo.com")
    assert guardian.call_count == 1 and "guardian" in result.providers
    item = next(a for a in result.articles if a.provider == "guardian")
    assert item.summary == "Interview with the boss" and item.publisher == "The Guardian"


# --- judging and the profile -----------------------------------------------------------


def judge(items):
    return httpx.Response(200, json={"done_reason": "stop", "message": {"content": json.dumps({"items": items})}})


@respx.mock
def test_research_news_judges_headlines_and_drops_unrelated(feeds, monkeypatch):
    monkeypatch.setenv("INFORMANT_LLM", "ollama")
    respx.post("http://localhost:11434/api/show").respond(json={"capabilities": []})
    chat = respx.post(OLLAMA).mock(return_value=judge([
        {"n": 1, "about_company": True, "sentiment": "neutral", "topic": "funding_or_deal"},
        {"n": 2, "about_company": True, "sentiment": "positive", "topic": "results_and_finances"},
        {"n": 3, "about_company": False, "sentiment": "neutral", "topic": "other"},
    ]))
    profile = CompanyProfile(query="x", identity=Identity(
        name="MONZO BANK LIMITED", website="https://monzo.com", description="A UK digital bank."))
    events = []
    research_news(profile, progress=events.append)

    news = profile.news
    assert [(i.title[:12], i.sentiment, i.topic, i.relevant) for i in news.items] == [
        ("Monzo in tal", 0.0, "funding_or_deal", True),
        ("Monzo's prof", 1.0, "results_and_finances", True),
        ("Monzo bank w", 0.0, "other", False)]  # doubtful: kept, but marked
    assert news.searched_for == ["monzo", "monzo bank"] and news.window_days == 90
    assert news.items[0].sources[0].url == "https://news.example/monzo-nubank"
    assert any("1 headline(s) marked relevant=false" in n for n in news.notes)
    sent = json.loads(chat.calls[0].request.content)["messages"][1]["content"]
    assert "1. Monzo in talks" in sent and "What it does: A UK digital bank." in sent
    assert [e.status for e in events] == ["started", "progress", "done"]


@respx.mock
def test_research_news_without_llm_keeps_everything_unjudged(feeds):
    profile = CompanyProfile(query="x", identity=Identity(name="MONZO BANK LIMITED",
                                                          website="https://monzo.com"))
    research_news(profile, use_llm=False)
    assert len(profile.news.items) == 3
    assert all(i.sentiment is None and i.topic is None for i in profile.news.items)
