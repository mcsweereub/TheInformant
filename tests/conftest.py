import pytest


@pytest.fixture(autouse=True)
def no_cache_by_default(monkeypatch, tmp_path_factory):
    """Tests must not share cached pages or LLM answers with each other (or leave a
    cache folder in the repo). Cache tests switch it back on with a temp folder."""
    monkeypatch.setenv("INFORMANT_CACHE", "off")
    monkeypatch.delenv("INFORMANT_CACHE_DIR", raising=False)
    monkeypatch.delenv("INFORMANT_CACHE_DAYS", raising=False)
    # Older tests mock only the core Companies House endpoints; the deeper registry
    # steps (accounts, charges, ownership...) are tested in test_registry_details.py.
    monkeypatch.setenv("INFORMANT_REGISTRY", "basic")
    # Likewise the sitemap lookup, which adds a request older site mocks don't have;
    # it is tested in test_structured.py.
    monkeypatch.setenv("INFORMANT_SITEMAP", "off")
    # News makes requests to outside feeds; it is tested in test_news.py.
    monkeypatch.setenv("INFORMANT_NEWS", "off")
    # Wikipedia likewise (tested in test_wikipedia.py); and never send the user's contact
    # details from tests, even if a test loads the real .env.
    monkeypatch.setenv("INFORMANT_WIKIPEDIA", "off")
    monkeypatch.setenv("INFORMANT_PDF_ACCOUNTS", "off")  # tested in test_accounts_pdf.py
    # Web search spends the user's Tavily allowance: never from tests (test_web_search.py
    # uses fakes).
    monkeypatch.setenv("INFORMANT_SEARCH", "off")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("INFORMANT_CONTACT", raising=False)
    # The other public-data sources (GLEIF, Contracts Finder, job boards, site feeds,
    # DNS/certificates/Wayback) are tested in test_public_data.py.
    monkeypatch.setenv("INFORMANT_SKIP", "all")
    monkeypatch.delenv("GUARDIAN_API_KEY", raising=False)
    # Land Registry: never the real key or the user's downloaded copy (test_property.py
    # builds small ones in temp folders).
    monkeypatch.delenv("LAND_REGISTRY_API_KEY", raising=False)
    monkeypatch.setenv("INFORMANT_DATA_DIR", str(tmp_path_factory.mktemp("informant_data")))
