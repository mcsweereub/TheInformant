"""Local cache of crawled pages and LLM answers."""

import json
from datetime import timedelta

import pytest
import respx
from pydantic import BaseModel
from typer.testing import CliRunner

from informant import cli
from informant.cache import LlmCache, PageCache, cache_stats, clear_cache
from informant.llm import extract_with_llm
from informant.llm.backends.base import Completion, with_cache
from informant.schema import utcnow
from informant.sources.companies_house import BASE_URL
from informant.sources.website.fetch import Crawler
from test_companies_house import FILINGS, OFFICERS, PROFILE, PSCS
from test_renderer import RICH, SHELL, FakeRenderer
from test_staged import LARGE, FakeOllama

HOME = ("<html><head><title>Acme</title></head><body><a href='/about'>About</a><p>"
        + "Acme makes widgets in Leeds. " * 40 + "</p></body></html>")
ABOUT = "<html><head><title>About</title></head><body><p>" + "About Acme. " * 40 + "</p></body></html>"


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("INFORMANT_CACHE", "on")
    monkeypatch.setenv("INFORMANT_CACHE_DIR", str(tmp_path / "cache"))
    return tmp_path / "cache"


def mock_site():
    return {
        "robots": respx.get("https://acme.example/robots.txt").respond(404),
        "home": respx.get("https://acme.example/").respond(html=HOME),
        "about": respx.get("https://acme.example/about").respond(html=ABOUT),
    }


# --- pages ----------------------------------------------------------------------


def test_page_cache_roundtrip_expiry_and_damage(cache_dir):
    cache = PageCache()
    now = utcnow()
    cache.put("https://x.example/", final_url="https://x.example/", status_code=200,
              html="<p>hi</p>", headers={"server": "nginx"}, rendered=True, fetched_at=now)
    hit = cache.get("https://x.example")  # trailing slash / case don't matter
    assert hit.html == "<p>hi</p>" and hit.rendered and hit.fetched_at == now
    assert PageCache(refresh=True).get("https://x.example/") is None

    cache.put("https://old.example/", final_url="https://old.example/", status_code=200, html="x",
              headers={}, rendered=False, fetched_at=now - timedelta(days=8))
    assert cache.get("https://old.example/") is None  # older than 7 days
    assert PageCache(max_age_days=30).get("https://old.example/") is not None

    cache._path("https://x.example/").write_bytes(b"not gzip")
    assert cache.get("https://x.example/") is None  # damaged entry is a miss, not a crash


@respx.mock
def test_second_crawl_makes_no_requests(cache_dir):
    routes = mock_site()
    with Crawler(delay=0, cache=PageCache()) as c:
        first = c.crawl("https://acme.example/")
    assert routes["home"].call_count == 1 and not any(p.from_cache for p in first.pages)

    with Crawler(delay=0, cache=PageCache()) as c:
        second = c.crawl("https://acme.example/")
    assert [r.call_count for r in routes.values()] == [1, 1, 1]  # nothing fetched again
    assert all(p.from_cache for p in second.ok_pages) and len(second.ok_pages) == 2
    assert second.ok_pages[0].fetched_at == first.ok_pages[0].fetched_at  # real download time
    assert second.ok_pages[0].text == first.ok_pages[0].text
    assert any("came from the local cache" in n for n in second.notes)

    with Crawler(delay=0, cache=PageCache(refresh=True)) as c:
        third = c.crawl("https://acme.example/")
    assert routes["home"].call_count == 2 and not any(p.from_cache for p in third.pages)


@respx.mock
def test_unrendered_shell_in_cache_is_not_reused_for_crawl(cache_dir):
    """Discovery probes cache the raw JavaScript shell; the crawl must still render it."""
    respx.get("https://bank.example/robots.txt").respond(404)
    home = respx.get("https://bank.example/").respond(html=SHELL)
    with Crawler(delay=0, cache=PageCache()) as c:
        assert not c.probe("bank.example").rendered  # caches the thin shell

    fake = FakeRenderer(pages={"https://bank.example/": RICH.replace("href='/about'", "href='#'")
                               .replace("href='/products'", "href='#'")})
    with Crawler(delay=0, cache=PageCache(), renderer=fake) as c:
        result = c.crawl("https://bank.example/", max_pages=1)
    assert result.ok_pages[0].rendered and fake.calls == ["https://bank.example/"]
    assert home.call_count == 1  # shell came from the cache; only the browser ran

    fake2 = FakeRenderer()
    with Crawler(delay=0, cache=PageCache(), renderer=fake2) as c:
        again = c.crawl("https://bank.example/", max_pages=1)
    assert again.ok_pages[0].rendered and again.ok_pages[0].from_cache and fake2.calls == []


# --- LLM answers ----------------------------------------------------------------


class Answer(BaseModel):
    value: str


def test_with_cache_reuses_only_identical_successful_calls(cache_dir):
    calls = []

    def backend(system, user, schema):
        calls.append(user)
        if user == "fail":
            return Completion(None, "m", ["boom"])
        return Completion(Answer(value=user.upper()), "m", [], 12.0)

    cached = with_cache(backend, LlmCache(), "model-a")
    assert cached("sys", "hello", Answer).data.value == "HELLO"
    again = cached("sys", "hello", Answer)
    assert again.cached and again.data.value == "HELLO" and calls == ["hello"]

    cached("sys", "other text", Answer)  # different text: real call
    cached("sys 2", "hello", Answer)  # different prompt: real call
    with_cache(backend, LlmCache(), "model-b")("sys", "hello", Answer)  # different model
    assert len(calls) == 4

    cached("sys", "fail", Answer)
    cached("sys", "fail", Answer)  # failures are never cached
    assert calls.count("fail") == 2


@respx.mock
def test_staged_rerun_uses_cached_answers(cache_dir, monkeypatch):
    monkeypatch.setenv("INFORMANT_LLM", "ollama")
    for k in ("INFORMANT_LLM_STRATEGY", "OLLAMA_MODEL", "OLLAMA_HOST"):
        monkeypatch.delenv(k, raising=False)
    fake = FakeOllama()
    first = extract_with_llm("Acme", LARGE, cache=LlmCache())
    made = len(fake.calls)
    assert made >= 3 and first.data is not None

    second = extract_with_llm("Acme", LARGE, cache=LlmCache())
    assert len(fake.calls) == made  # no new model calls at all
    assert second.data.summary == first.data.summary
    assert any(f"{made} answered from the local cache" in n for n in second.notes)

    monkeypatch.setenv("OLLAMA_MODEL", "other-model:1b")  # a different model starts afresh
    extract_with_llm("Acme", LARGE, cache=LlmCache())
    assert len(fake.calls) == 2 * made


# --- CLI ------------------------------------------------------------------------


def invoke(*args):
    return CliRunner().invoke(cli.app, list(args))


@respx.mock
def test_lookup_uses_cache_and_flags(cache_dir, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "k")
    respx.get(f"{BASE_URL}/company/01234567").respond(json=PROFILE)
    respx.get(f"{BASE_URL}/company/01234567/officers").respond(json=OFFICERS)
    respx.get(f"{BASE_URL}/company/01234567/persons-with-significant-control").respond(json=PSCS)
    respx.get(f"{BASE_URL}/company/01234567/filing-history").respond(json=FILINGS)
    routes = mock_site()
    args = ("lookup", "01234567", "--website", "https://acme.example/", "--no-llm", "--json")

    first = json.loads(invoke(*args).stdout)
    assert [p["cached"] for p in first["profile"]["website"]["pages"]] == [False, False]
    second = json.loads(invoke(*args).stdout)
    pages = second["profile"]["website"]["pages"]
    assert [p["cached"] for p in pages] == [True, True] and routes["home"].call_count == 1
    assert pages[0]["fetched_at"] == first["profile"]["website"]["pages"][0]["fetched_at"]
    assert second["profile"]["website"]["sources"][0]["retrieved_at"] == pages[0]["fetched_at"]

    invoke(*args, "--refresh")
    assert routes["home"].call_count == 2
    third = json.loads(invoke(*args, "--no-cache").stdout)
    assert routes["home"].call_count == 3
    assert [p["cached"] for p in third["profile"]["website"]["pages"]] == [False, False]


def test_cache_command_stats_and_clear(cache_dir):
    PageCache().put("https://x.example/", final_url="https://x.example/", status_code=200,
                    html="<p>hi</p>", headers={}, rendered=False, fetched_at=utcnow())
    LlmCache().put("abc", {"data": {"value": "x"}})
    stats = json.loads(invoke("cache", "--json").stdout)
    assert stats["pages"]["entries"] == 1 and stats["llm"]["entries"] == 1 and stats["enabled"]

    cleared = json.loads(invoke("cache", "--clear", "--pages", "--json").stdout)
    assert cleared["removed"] == 1 and cleared["pages"]["entries"] == 0
    assert cleared["llm"]["entries"] == 1
    assert clear_cache() == 1 and cache_stats()["llm"]["entries"] == 0
