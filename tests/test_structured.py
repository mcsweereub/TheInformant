"""Structured data a site declares about itself (JSON-LD, OpenGraph), and
finding key pages through the sitemap."""

import json

import pytest
import respx

from informant.cache import PageCache
from informant.schema import CompanyProfile, Identity, Location
from informant.sources.website.fetch import Crawler
from informant.sources.website.pages import choose_pages, sitemap_candidates
from informant.sources.website.parse import parse_page
from informant.sources.website.research import research_website
from informant.sources.website.structured import extract_structured

ORG = {
    "@context": "https://schema.org",
    "@graph": [
        {"@type": "WebSite", "name": "Acme"},
        {"@type": ["Organization", "Bakery"], "name": "Acme Bakeries", "legalName": "Acme Bakeries Limited",
         "description": "Family bakers since 1951.", "foundingDate": "1951-04-02",
         "logo": {"@type": "ImageObject", "url": "https://acme.example/logo.png"},
         "numberOfEmployees": {"@type": "QuantitativeValue", "minValue": 51, "maxValue": 200},
         "vatID": "GB123456789", "parentOrganization": {"@type": "Organization", "name": "Acme Group"},
         "telephone": "+44 20 7946 0000", "email": "hello@acme.example",
         "sameAs": ["https://www.linkedin.com/company/acme-bakeries",
                    "https://www.linkedin.com/in/jane-founder",  # a person: must be ignored
                    "https://www.instagram.com/acmebakeries"],
         "address": {"@type": "PostalAddress", "streetAddress": "1 High Street",
                     "addressLocality": "Leeds", "postalCode": "LS1 1AA", "addressCountry": "GB"},
         "contactPoint": [{"@type": "ContactPoint", "telephone": "0800 111 2222",
                           "email": "jane.founder@acme.example"}],
         "founder": {"@type": "Person", "name": "Jane Founder"}},
        {"@type": "Person", "name": "Jane Founder", "jobTitle": "CEO"},
    ],
}


def page_with(ld=None, extra_head="", body="<p>Welcome to Acme Bakeries of Leeds.</p>", url="https://acme.example/"):
    scripts = "".join(f'<script type="application/ld+json">{json.dumps(x) if not isinstance(x, str) else x}</script>'
                      for x in (ld or []))
    return parse_page(url, "home", 200,
                      f"<html><head><title>Acme</title>{extra_head}{scripts}</head><body>{body}</body></html>")


def mock_single_page_site():
    """A site with one page carrying the ORG markup (and no about page)."""
    respx.get("https://acme.example/robots.txt").respond(404)
    respx.get("https://acme.example/").respond(html=page_with([ORG]).html)
    respx.get("https://acme.example/about").respond(404)
    respx.get("https://acme.example/about-us").respond(404)


def test_organisation_facts_from_json_ld():
    facts = extract_structured([page_with([ORG])])
    assert facts.name == "Acme"  # WebSite name seen first; legal name kept separately
    assert facts.legal_name == "Acme Bakeries Limited"
    assert facts.description == "Family bakers since 1951." and facts.founding_date == "1951-04-02"
    assert facts.logo_url == "https://acme.example/logo.png"
    assert facts.employees == "51-200" and facts.vat_id == "GB123456789"
    assert facts.parent_organisation == "Acme Group"
    assert facts.schema_types == ["Organization", "Bakery"]
    assert facts.addresses == [{"street": "1 High Street", "locality": "Leeds", "region": None,
                                "postal_code": "LS1 1AA", "country": "GB"}]
    assert "Jane Founder" not in facts.as_text()  # people in the markup are not read
    assert "Founded: 1951-04-02" in facts.as_text() and "Address: 1 High Street, Leeds" in facts.as_text()


def test_broken_json_ld_and_opengraph_fallback():
    og = ('<meta property="og:site_name" content="Greggs">'
          '<meta property="og:description" content="Sandwiches and pasties.">'
          '<meta property="og:image" content="https://g.example/logo.jpg">')
    facts = extract_structured([page_with(["{not valid json", {"@type": "Product", "name": "Pasty"}],
                                          extra_head=og)])
    assert facts.name == "Greggs" and facts.description == "Sandwiches and pasties."
    assert facts.logo_url == "https://g.example/logo.jpg" and facts.schema_types == []
    assert extract_structured([page_with()]).is_empty()


@respx.mock
def test_structured_data_merges_into_profile_with_the_usual_rules():
    mock_single_page_site()
    profile = CompanyProfile(
        query="01234567", identity=Identity(name="ACME BAKERIES LIMITED", company_number="01234567"),
        locations=[Location(kind="registered_office", postal_code="EC1A 1AA")])
    with Crawler(delay=0) as c:
        research_website(profile, crawler=c, user_url="https://acme.example/", use_llm=False)

    site = profile.website
    assert site.structured.legal_name == "Acme Bakeries Limited"
    assert site.structured.founding_date == "1951-04-02" and site.structured.employees == "51-200"
    assert site.emails == ["hello@acme.example"]  # the personal address is not kept
    assert "+442079460000" in site.phones and "08001112222" in site.phones
    assert site.social_links == {"linkedin": "https://www.linkedin.com/company/acme-bakeries",
                                 "instagram": "https://www.instagram.com/acmebakeries"}
    office = [l for l in profile.locations if l.kind == "office"]
    assert len(office) == 1 and office[0].postal_code == "LS1 1AA"
    assert office[0].sources[0].name == "website"
    # With no LLM, the site's own description fills the summary.
    assert profile.identity.description == "Family bakers since 1951."


@respx.mock
def test_declared_address_matching_registered_office_is_not_duplicated():
    mock_single_page_site()
    profile = CompanyProfile(
        query="x", identity=Identity(name="ACME BAKERIES LIMITED"),
        locations=[Location(kind="registered_office", postal_code="ls1 1aa")])
    with Crawler(delay=0) as c:
        research_website(profile, crawler=c, user_url="https://acme.example/", use_llm=False)
    assert [l.kind for l in profile.locations] == ["registered_office"]


@respx.mock
def test_llm_receives_the_declared_facts_first(monkeypatch):
    monkeypatch.setenv("INFORMANT_LLM", "ollama")
    mock_single_page_site()
    respx.post("http://localhost:11434/api/show").respond(json={"capabilities": []})
    chat = respx.post("http://localhost:11434/api/chat").respond(json={"done_reason": "stop", "message": {
        "content": json.dumps({"summary": "Acme Bakeries is a family baker in Leeds.",
                               "business_model": "B2C", "sectors": ["Food & Drink Retail"],
                               "tags": ["bakery"], "target_markets": [], "products": [], "offices": [],
                               "key_facts": [], "milestones": [], "funding_rounds": [],
                               "shareholders": []})}})
    profile = CompanyProfile(query="x", identity=Identity(name="ACME BAKERIES LIMITED"))
    with Crawler(delay=0) as c:
        research_website(profile, crawler=c, user_url="https://acme.example/")
    sent = json.loads(chat.calls[0].request.content)["messages"][1]["content"]
    assert sent.index("=== PAGE (structured)") < sent.index("=== PAGE (home)")
    assert "Founded: 1951-04-02" in sent and "Jane Founder" not in sent
    assert profile.identity.description.startswith("Acme Bakeries is")  # LLM summary wins


# --- sitemap ---------------------------------------------------------------------------------

HOME = ("<html><head><title>Acme</title></head><body><nav><a href='/products'>Products</a></nav><p>"
        + "Acme makes widgets in Leeds. " * 40 + "</p></body></html>")
PAGE = "<html><head><title>{t}</title></head><body><p>{t} page for Acme.</p></body></html>"
SITEMAP = """<?xml version="1.0"?><urlset>
 <url><loc>https://acme.example/</loc></url>
 <url><loc>https://acme.example/products</loc></url>
 <url><loc> https://acme.example/who-we-are </loc></url>
 <url><loc><![CDATA[https://acme.example/investor-relations?x=1&amp;y=2]]></loc></url>
 <url><loc>https://acme.example/fr/about</loc></url>
 <url><loc>https://acme.example/blog/2024/05/about-our-year</loc></url>
 <url><loc>https://other.example/about</loc></url>
 <url><loc>https://acme.example/privacy-policy</loc></url>
</urlset>"""


def test_sitemap_candidates_filter_and_choice():
    home = parse_page("https://acme.example/", "home", 200, HOME)
    urls = ["https://acme.example/who-we-are", "https://acme.example/fr/about",
            "https://acme.example/blog/2024/05/about-our-year", "https://other.example/about",
            "https://acme.example/investor-relations"]
    links = sitemap_candidates(urls, home.url)
    assert [u for u, _ in links] == ["https://acme.example/who-we-are",
                                     "https://acme.example/investor-relations"]
    chosen = choose_pages(home, 5, links=links, kinds=["about", "investors", "careers"])
    assert dict((k, u) for u, k in chosen) == {"about": "https://acme.example/who-we-are",
                                               "investors": "https://acme.example/investor-relations"}


@pytest.fixture
def sitemap_on(monkeypatch):
    monkeypatch.setenv("INFORMANT_SITEMAP", "on")


@respx.mock
def test_crawl_finds_unlinked_pages_through_the_sitemap(sitemap_on):
    respx.get("https://acme.example/robots.txt").respond(
        text="User-agent: *\nAllow: /\nSitemap: https://acme.example/sitemap_index.xml\n")
    respx.get("https://acme.example/sitemap_index.xml").respond(
        text="<sitemapindex><sitemap><loc>https://acme.example/post-sitemap.xml</loc></sitemap>"
             "<sitemap><loc>https://acme.example/page-sitemap.xml</loc></sitemap></sitemapindex>")
    pages_map = respx.get("https://acme.example/page-sitemap.xml").respond(text=SITEMAP)
    respx.get("https://acme.example/post-sitemap.xml").respond(text="<urlset></urlset>")
    respx.get("https://acme.example/").respond(html=HOME)
    for path, title in (("/products", "Products"), ("/who-we-are", "Who we are")):
        respx.get(f"https://acme.example{path}").respond(html=PAGE.format(t=title))
    respx.get("https://acme.example/investor-relations?x=1&y=2").respond(html=PAGE.format(t="Investors"))

    with Crawler(delay=0) as c:
        result = c.crawl("https://acme.example/")
    kinds = {p.kind: p.url for p in result.ok_pages}
    assert kinds["products"] == "https://acme.example/products"  # from the homepage link
    assert kinds["about"] == "https://acme.example/who-we-are"  # only in the sitemap
    assert kinds["investors"].startswith("https://acme.example/investor-relations")
    assert pages_map.call_count == 1
    assert any("found through the site's sitemap: about, investors" in n for n in result.notes)
    assert not any("privacy" in p.url or "/fr/" in p.url or "/blog/" in p.url for p in result.pages)


@respx.mock
def test_missing_sitemap_is_harmless_and_remembered(sitemap_on, tmp_path):
    robots = respx.get("https://acme.example/robots.txt").respond(404)
    sitemap = respx.get("https://acme.example/sitemap.xml").respond(404)
    respx.get("https://acme.example/").respond(html=HOME)
    respx.get("https://acme.example/products").respond(html=PAGE.format(t="Products"))
    respx.get("https://acme.example/about").respond(404)
    respx.get("https://acme.example/about-us").respond(404)

    for _ in range(2):
        with Crawler(delay=0, cache=PageCache(tmp_path)) as c:
            result = c.crawl("https://acme.example/")
        assert [p.kind for p in result.ok_pages] == ["home", "products"]
    # Second run: robots.txt and the missing sitemap both came from the cache.
    assert sitemap.call_count == 1 and robots.call_count == 1


@respx.mock
def test_sitemap_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("INFORMANT_SITEMAP", "off")
    respx.get("https://acme.example/robots.txt").respond(404)
    respx.get("https://acme.example/").respond(html=HOME)
    respx.get("https://acme.example/products").respond(html=PAGE.format(t="Products"))
    respx.get("https://acme.example/about").respond(404)
    respx.get("https://acme.example/about-us").respond(404)
    with Crawler(delay=0) as c:
        c.crawl("https://acme.example/")  # would fail on the unmocked /sitemap.xml if it tried


@respx.mock
def test_utf8_pages_with_a_wrong_charset_label_are_decoded_correctly():
    body = "<html><head><title>T</title></head><body><p>154 – 156 Blackfriars Road, world’s best, £3</p></body></html>"
    respx.get("https://acme.example/robots.txt").respond(404)
    respx.get("https://acme.example/").respond(
        content=body.encode("utf-8"), headers={"content-type": "text/html; charset=ISO-8859-1"})
    respx.get("https://latin.example/robots.txt").respond(404)
    respx.get("https://latin.example/").respond(
        content="<html><body><p>Café £3</p></body></html>".encode("latin-1"),
        headers={"content-type": "text/html; charset=ISO-8859-1"})
    with Crawler(delay=0) as c:
        page = c.probe("https://acme.example/")
        latin = c.probe("https://latin.example/")
    assert "154 – 156 Blackfriars Road, world’s best, £3" in page.text
    assert "Café £3" in latin.text  # genuinely Latin-1 pages still work
