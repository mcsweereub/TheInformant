"""The extra public-data sources: GLEIF, Contracts Finder, job boards, site feeds, domain
signals (DNS, certificate logs, Wayback) and shared analytics IDs. HTTP is mocked."""

import httpx
import pytest
import respx

from informant.errors import ConfigError
from informant.pipeline import research_company, skipped_sources
from informant.schema import CompanyProfile, Identity, Website
from informant.sources.contracts import SEARCH_URL, contracts_material, fetch_contracts, is_supplier
from informant.sources.domain import CERTSPOTTER_URL, DOH_URL, WAYBACK_CDX, fetch_domain_signals, registrable
from informant.sources.gleif import API as GLEIF, fetch_lei
from informant.sources.jobs import Board, fetch_jobs, find_boards, jobs_material
from informant.sources.website.extract import extract_analytics_ids
from informant.sources.website.feeds import feeds_material, shopify_catalogue, wordpress_posts
from informant.sources.website.fetch import Crawler
from informant.sources.website.parse import Page
from informant.storage import load_index, save_profile
from test_cli_json import NUMBER, mock_companies_house

# --- GLEIF --------------------------------------------------------------------------------


def lei_record(lei, name, number="00502851", country="GB", rels=()):
    return {"id": lei, "attributes": {
        "entity": {"legalName": {"name": name}, "otherNames": [{"name": "Trading Name"}],
                   "legalAddress": {"country": country},
                   "headquartersAddress": {"city": "NEWCASTLE", "country": country},
                   "registeredAs": number, "jurisdiction": country, "status": "ACTIVE"},
        "registration": {"status": "ISSUED", "initialRegistrationDate": "2017-01-09T00:00:00Z",
                         "nextRenewalDate": "2027-01-09T00:00:00Z"},
        "bic": None},
        "relationships": {r: None for r in rels}}  # single records often omit the links


@respx.mock
def test_lei_by_number_with_parent_subsidiaries_and_isins():
    lei = "LEI0000000000000AAAA"
    respx.get(f"{GLEIF}/lei-records", params={"filter[entity.registeredAs]": "00502851"}).respond(
        json={"data": [lei_record(lei, "GREGGS PLC", rels=("direct-parent", "ultimate-parent",
                                                           "direct-children", "isins"))]})
    respx.get(f"{GLEIF}/lei-records/{lei}/direct-parent").respond(404)  # none reported
    respx.get(f"{GLEIF}/lei-records/{lei}/ultimate-parent").respond(
        json={"data": lei_record("PARENT00000000000000", "BIG HOLDINGS INC", country="US")})
    respx.get(f"{GLEIF}/lei-records/{lei}/direct-parent-reporting-exception").respond(
        json={"data": {"attributes": {"reason": "NON_CONSOLIDATING"}}})
    respx.get(f"{GLEIF}/lei-records/{lei}/direct-children").respond(
        json={"data": [lei_record("CHILD000000000000000", "GREGGS FRANCE SAS", country="FR")],
              "meta": {"pagination": {"total": 1}}})
    respx.get(f"{GLEIF}/lei-records/{lei}/isins").respond(
        json={"data": [{"attributes": {"isin": "GB00B63QSB39"}}]})

    s = fetch_lei("00502851", "GREGGS PLC").section
    assert (s.lei, s.matched_by, s.registration_status) == (lei, "company_number", "ISSUED")
    assert s.ultimate_parent.name == "BIG HOLDINGS INC" and s.ultimate_parent.country == "US"
    assert s.direct_parent is None and s.parent_exception == "NON_CONSOLIDATING"
    assert [c.name for c in s.subsidiaries] == ["GREGGS FRANCE SAS"] and s.subsidiaries_total == 1
    assert s.isins == ["GB00B63QSB39"] and s.headquarters == "NEWCASTLE, GB"
    assert s.other_names == ["Trading Name"] and s.sources[0].name == "gleif"


@respx.mock
def test_lei_falls_back_to_an_exact_uk_name():
    # Banks register their LEI against the FCA, not Companies House; GLEIF's name filter
    # is fuzzy, so only an exact match on the name without suffixes is accepted.
    def search(request):
        if "filter[entity.registeredAs]" in request.url.params:
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"data": [
            lei_record("HOLDING0000000000000", "MONZO BANK HOLDING GROUP LIMITED", number="14785367"),
            lei_record("BANK0000000000000000", "MONZO BANK LTD", number="730427")]})
    respx.get(f"{GLEIF}/lei-records").mock(side_effect=search)
    respx.get(url__regex=rf"{GLEIF}/lei-records/BANK.*/direct-parent-reporting-exception").respond(404)
    s = fetch_lei("09446231", "MONZO BANK LIMITED").section
    assert (s.lei, s.matched_by) == ("BANK0000000000000000", "name")


@respx.mock
def test_no_lei_and_failures_are_notes():
    respx.get(f"{GLEIF}/lei-records").respond(json={"data": []})
    s = fetch_lei("09461711", "PLANLOGIC SOFTWARE LIMITED").section
    assert s.lei is None and "no LEI" in s.notes[0]
    respx.get(f"{GLEIF}/lei-records").respond(500)
    assert "failed" in fetch_lei("1", "X LTD").section.notes[0]


# --- Contracts Finder ------------------------------------------------------------------------


def notice(id, supplier, value, awarded, buyer="Home Office", title="IT kit"):
    return {"item": {"id": id, "title": title, "organisationName": buyer, "awardedSupplier": supplier,
                     "awardedValue": value, "awardedDate": awarded, "cpvDescription": "IT services",
                     "regionText": "London", "start": None, "end": None}}


def test_supplier_name_variants():
    for name in ("SOFTCAT PLC", "Softcat Ltd (Head Office)", "SOFTCAT LTD - FCA", "SoftCat", "softcat plc "):
        assert is_supplier(name, "softcat"), name
    for name in ("Allocate (Softcat PLC)", "Softcat Holdings Europe", "Softcat Group Ltd", "Socitm Ltd"):
        assert not is_supplier(name, "softcat"), name


@respx.mock
def test_contracts_keep_only_awards_to_the_company():
    route = respx.post(SEARCH_URL).respond(json={"hitCount": 4, "noticeList": [
        notice("a", "SOFTCAT PLC", 100_000, "2024-05-01T00:00:00Z"),
        notice("b", "Bechtle Ltd,Softcat plc,CDW Limited", 9_000_000, "2025-01-01T00:00:00Z",
               buyer="Crown Commercial Service", title="Technology framework"),
        notice("c", "Jamieson Contracting (NW) Ltd", 5_000, "2023-01-01T00:00:00Z"),  # keyword only
        notice("d", "Softcat Ltd", 50_000, "2022-03-01T00:00:00Z", buyer="NHS Trust"),
    ]})
    pc = fetch_contracts("SOFTCAT PLC")
    assert b'"keyword":"\\"Softcat\\""' in route.calls[0].request.content.replace(b' ', b'')
    assert [c.url.rsplit("/", 1)[-1] for c in pc.items] == ["b", "a", "d"]  # newest first
    assert pc.found == 3 and pc.items[0].suppliers == 3
    assert pc.sole_supplier_value == 150_000  # the framework's £9m isn't this company's
    assert pc.top_buyers == {"Crown Commercial Service": 1, "Home Office": 1, "NHS Trust": 1}
    material = contracts_material(pc)
    assert "Home Office" in material.text and "shared with 2 other suppliers" in material.text


# --- job boards -------------------------------------------------------------------------------


def test_find_boards_in_links_and_embeds():
    html = ['<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>']
    links = ["https://jobs.lever.co/acme-other/123", "https://boards.greenhouse.io/acme/jobs/1"]
    boards = find_boards(html, links)
    assert (boards[0].provider, boards[0].token) == ("greenhouse", "acme")  # mentioned twice
    assert ("lever", "acme-other") in [(b.provider, b.token) for b in boards]


@respx.mock
def test_greenhouse_roles_deduplicated_and_summarised():
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/departments").respond(json={"departments": [
        {"name": "Engineering", "jobs": [
            {"title": "Backend Engineer", "location": {"name": "London"}, "absolute_url": "https://g/1",
             "updated_at": "2026-10-01T10:00:00-04:00"},
            {"title": "Data Engineer", "location": {"name": "Remote"}, "absolute_url": "https://g/2"}]},
        {"name": "Platform", "jobs": [  # the same role listed under a second department
            {"title": "Backend Engineer", "location": {"name": "London"}, "absolute_url": "https://g/1"}]},
    ]})
    h = fetch_jobs("ACME LIMITED", boards=[Board("greenhouse", "acme", "website")], names=["acme"])
    assert (h.provider, h.total, h.found_via) == ("greenhouse", 2, "website")
    assert h.departments == {"Engineering": 2} and h.locations == {"London": 1, "Remote": 1}
    assert h.open_roles[0].title == "Backend Engineer"  # newest first
    assert "Engineering (2)" in jobs_material(h).text


@respx.mock
def test_board_found_by_name_must_name_the_employer():
    respx.get("https://boards-api.greenhouse.io/v1/boards/monzo").respond(json={"name": "Monzo"})
    respx.get("https://boards-api.greenhouse.io/v1/boards/monzo/departments").respond(
        json={"departments": [{"name": "Ops", "jobs": [{"title": "Analyst", "absolute_url": "u"}]}]})
    respx.get("https://boards-api.greenhouse.io/v1/boards/monzobank").respond(404)
    respx.get(url__regex=r"https://apply\.workable\.com/.*").respond(404)
    h = fetch_jobs("MONZO BANK LIMITED", boards=[], names=["monzo", "monzo bank"])
    assert (h.provider, h.found_via, h.total) == ("greenhouse", "name", 1)

    respx.get("https://boards-api.greenhouse.io/v1/boards/acme").respond(json={"name": "Acme Rockets Inc"})
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/departments").respond(json={"departments": []})
    other = fetch_jobs("ACME LIMITED", boards=[], names=["acme"])
    assert other.provider is None and other.notes == ["no public job board found"]


@respx.mock
def test_lever_board_from_the_site():
    respx.get("https://api.lever.co/v0/postings/acme", params={"mode": "json"}).respond(json=[
        {"text": "Designer", "categories": {"team": "Design", "location": "Leeds"},
         "hostedUrl": "https://jobs.lever.co/acme/1", "createdAt": 1767225600000}])
    h = fetch_jobs("ACME LIMITED", boards=[Board("lever", "acme", "website")], names=[])
    assert h.open_roles[0].department == "Design" and str(h.open_roles[0].posted_at) == "2026-01-01"


# --- site feeds --------------------------------------------------------------------------------


@respx.mock
def test_wordpress_posts_and_shopify_catalogue():
    respx.get("https://acme.example/robots.txt").respond(404)
    respx.get(url__startswith="https://acme.example/wp-json/wp/v2/posts").respond(json=[
        {"date": "2026-09-01T09:00:00", "link": "https://acme.example/news/a",
         "title": {"rendered": "Acme wins &amp; expands"}, "excerpt": {"rendered": "<p>Big news.</p>"}}])
    respx.get(url__startswith="https://acme.example/products.json").respond(json={"products": [
        {"title": "Red Mug", "product_type": "Mugs", "vendor": "Acme", "published_at": "2026-01-01",
         "variants": [{"price": "8.00"}, {"price": "12.50"}]},
        {"title": "Tea Towel", "product_type": "Textiles", "vendor": "Acme", "published_at": "2026-05-01",
         "variants": [{"price": "6.00"}]}]})
    with Crawler(delay=0, sitemap=False) as c:
        posts = wordpress_posts(c, "https://acme.example/")
        shop = shopify_catalogue(c, "https://acme.example/")
    assert posts[0].title == "Acme wins & expands" and posts[0].excerpt == "Big news."
    assert shop.products_listed == 2 and (shop.price_min, shop.price_max) == (6.0, 12.5)
    assert shop.newest == ["Tea Towel", "Red Mug"] and shop.product_types == {"Mugs": 1, "Textiles": 1}
    text = "\n".join(p.text for p in feeds_material(posts, shop, "https://acme.example/"))
    assert "Acme wins & expands" in text and "Mugs (1)" in text


@respx.mock
def test_feeds_respect_robots_txt():
    respx.get("https://acme.example/robots.txt").respond(text="User-agent: *\nDisallow: /wp-json/\n")
    feed = respx.get(url__startswith="https://acme.example/wp-json/").respond(json=[])
    with Crawler(delay=0, sitemap=False) as c:
        assert wordpress_posts(c, "https://acme.example/") == []
    assert not feed.called


# --- domain signals --------------------------------------------------------------------------------


def dns_answer(rtype, *values):
    code = {"MX": 15, "TXT": 16}[rtype]
    return {"Status": 0, "Answer": [{"type": code, "data": v} for v in values]}


@respx.mock
def test_domain_signals():
    def dns(request):
        name, rtype = request.url.params["name"], request.url.params["type"]
        answers = {
            ("acme.co.uk", "MX"): dns_answer("MX", "0 acme-co-uk.mail.protection.outlook.com."),
            ("acme.co.uk", "TXT"): dns_answer(
                "TXT", '"MS=ms123"', '"v=spf1 include:spf.protection.outlook.com include:servers.mcsv.net -all"',
                '"atlassian-domain-verification=abc"', '"google-site-verification=xyz"'),
            ("_dmarc.acme.co.uk", "TXT"): dns_answer("TXT", '"v=DMARC1; p=reject; rua=mailto:x@acme.co.uk"'),
        }
        return httpx.Response(200, json=answers[(name, rtype)])
    respx.get(DOH_URL).mock(side_effect=dns)
    respx.get(url__startswith=CERTSPOTTER_URL).respond(json=[
        {"dns_names": ["acme.co.uk", "*.acme.co.uk", "portal.acme.co.uk", "dashboard.bigclient.acme.co.uk"],
         "not_before": "2016-03-01T00:00:00Z"},
        {"dns_names": ["acme-group.com", "www.acme-group.com"], "not_before": "2019-01-01T00:00:00Z"}])

    def wayback(request):
        rows = [["timestamp"], ["20020101000000"]] if request.url.params["limit"] == "1" \
            else [["timestamp"], ["20261101000000"]]
        return httpx.Response(200, json=rows)
    respx.get(WAYBACK_CDX).mock(side_effect=wayback)

    from datetime import date
    d = fetch_domain_signals("https://www.acme.co.uk/about", incorporated_on=date(2015, 6, 1))
    assert d.name == "acme.co.uk" and d.email_provider == "Microsoft 365"
    assert d.email_services == ["Microsoft 365", "Mailchimp"]
    assert d.verified_services == ["Microsoft 365", "Atlassian", "Google Search Console"]
    assert d.email_security == ["SPF: strict (-all)", "DMARC policy: reject"]
    assert d.subdomains == ["dashboard.bigclient.acme.co.uk", "portal.acme.co.uk"]
    assert d.other_domains == ["acme-group.com"]
    assert (str(d.first_archived), str(d.last_archived)) == ("2002-01-01", "2026-11-01")
    assert any("earlier owner" in n for n in d.notes)  # archived years before incorporation


@respx.mock
def test_domain_parts_fail_independently():
    respx.get(DOH_URL).respond(json={"Status": 0})  # no records
    respx.get(url__startswith=CERTSPOTTER_URL).respond(429)
    respx.get(WAYBACK_CDX).respond(json=[])
    d = fetch_domain_signals("https://acme.example")
    assert d.email_provider is None and d.subdomains == []
    assert d.notes == ["certificate logs lookup failed (rate limited, try again later)"]


def test_registrable_domains():
    assert registrable("a.b.example.co.uk") == "example.co.uk"
    assert registrable("api.monzo.com") == "monzo.com"
    assert registrable("monzo.me") == "monzo.me"


# --- analytics IDs and the index ---------------------------------------------------------------------


def test_analytics_ids():
    html = """<script async src="https://www.googletagmanager.com/gtag/js?id=G-AB12CD34EF"></script>
    <script>(function(w,d,s,l,i){})(window,document,'script','dataLayer','GTM-ABC1234');
    fbq('init', '123456789012345'); hjid:1234567</script>
    <script src="//js-eu1.hs-scripts.com/2345678.js"></script> UA-1234567-2 G-NOTANID"""
    ids = extract_analytics_ids([Page(url="u", kind="home", status_code=200, html=html)])
    assert ids == ["GTM-ABC1234", "UA-1234567-2", "G-AB12CD34EF", "meta-pixel:123456789012345",
                   "hotjar:1234567", "hubspot:2345678"]


def test_index_links_sites_sharing_an_analytics_id(tmp_path):
    def profile(number, name, ids):
        return CompanyProfile(query=number, identity=Identity(name=name, company_number=number),
                              website=Website(analytics_ids=ids))
    save_profile(profile("1", "ALPHA LTD", ["GTM-AAA1111", "G-SHARED123"]), out_dir=tmp_path)
    save_profile(profile("2", "BETA LTD", ["G-SHARED123"]), out_dir=tmp_path)
    save_profile(profile("3", "GAMMA LTD", ["GTM-OTHER11"]), out_dir=tmp_path)
    index = {c["company_number"]: c for c in load_index(tmp_path)["companies"]}
    assert index["1"]["shares_analytics_with"] == [
        {"name": "BETA LTD", "company_number": "2", "ids": ["G-SHARED123"]}]
    assert index["2"]["shares_analytics_with"][0]["company_number"] == "1"
    assert index["3"]["shares_analytics_with"] == []


# --- switching sources off, and the whole lookup ------------------------------------------------------------


def test_skipped_sources(monkeypatch):
    monkeypatch.setenv("INFORMANT_SKIP", "jobs, domain")
    assert skipped_sources() == {"jobs", "domain"}
    assert skipped_sources(["gleif,contracts", "feeds"]) == {"gleif", "contracts", "feeds"}
    assert skipped_sources(["all"]) == {"gleif", "contracts", "donations", "property", "jobs", "feeds", "domain"}
    assert skipped_sources([]) == set()
    with pytest.raises(ConfigError):
        skipped_sources(["linkedin"])


@respx.mock
def test_lookup_fills_the_public_record_sections(monkeypatch):
    mock_companies_house()
    respx.get(f"{GLEIF}/lei-records").respond(json={"data": []})
    respx.get("https://search.electoralcommission.org.uk/api/search/Donations").respond(
        json={"Total": 0, "Result": []})
    respx.post(SEARCH_URL).respond(json={"hitCount": 1, "noticeList": [
        notice("a", "Example Trading Ltd", 20_000, "2025-02-01T00:00:00Z")]})
    respx.get(url__startswith="https://boards-api.greenhouse.io/").respond(404)
    respx.get(url__startswith="https://apply.workable.com/").respond(404)
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "test-key")
    result = research_company(NUMBER, web=False, llm=False, skip=["domain"])
    p = result.profile
    assert p.public_contracts.found == 1 and p.public_contracts.items[0].buyer == "Home Office"
    assert p.lei.notes and p.hiring.notes == ["no public job board found"]
    assert p.domain.name is None  # skipped
