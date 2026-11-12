import pytest


@pytest.fixture(autouse=True)
def no_cache_by_default(monkeypatch):
    """Tests must not share cached pages or LLM answers with each other (or leave a
    cache folder in the repo). Cache tests switch it back on with a temp folder."""
    monkeypatch.setenv("INFORMANT_CACHE", "off")
    monkeypatch.delenv("INFORMANT_CACHE_DIR", raising=False)
    monkeypatch.delenv("INFORMANT_CACHE_DAYS", raising=False)
    # Older tests mock only the core Companies House endpoints; the deeper registry
    # steps (accounts, charges, ownership...) are tested in test_registry_details.py.
    monkeypatch.setenv("INFORMANT_REGISTRY", "basic")
