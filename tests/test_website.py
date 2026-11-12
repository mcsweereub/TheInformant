import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import anthropic
import httpx
import respx

from informant.sources.website.fetch import Crawler
from informant.sources.website.pages import choose_pages
from informant.sources.website.parse import parse_page
from informant.sources.website.discovery import candidate_domains, discover_website
from informant.sources.website.extract import (
    core_name,
    detect_tech,
    extract_contacts,
    extract_social_links,
    verify_ownership,
)
from informant.llm import SECTORS, extract_with_llm
from informant.schema import CompanyProfile, Identity, Location
from informant.sources.website.research import research_website

HOME = """<html><head><title>Acme Widgets | Industrial widgets</title>
<meta name="description" content="Widgets for industry.">
<script src="/wp-content/themes/x/app.js"></script>
<script src="https://www.googletagmanager.com/gtm.js?id=GTM-ABC123"></script></head>
<body><nav><a href="/about-us">About us</a> <a href="/products/">Our products</a>
<a href="/about-us/team/jane-smith">Jane</a> <a href="/careers">Careers</a>
<a href="/contact">Contact</a> <a href="https://other.example/about">Elsewhere</a>
<a href="/brochure.pdf">Brochure (products)</a></nav>
<p>We make widgets for factories across the UK.</p>
<a href="mailto:info@acme.example">Email</a> <a href="mailto:jane.smith@acme.example">Jane</a>
<a href="tel:+44 (0)20 7946 0000">Call</a>
<a href="https://www.linkedin.com/company/acme-widgets/">LinkedIn</a>
<a href="https://www.linkedin.com/in/jane-smith-123">Jane on LinkedIn</a>
<a href="https://twitter.com/acmewidgets">X</a>
<footer>Acme Widgets Ltd, registered in England no. 01234567. 1 High St, London EC1A 1AA</footer>
</body></html>"""

PAGE = "<html><head><title>{t}</title></head><body><p>{t} page text for Acme.</p></body></html>"


def _mock_site(robots: str = "User-agent: *\nDisallow: /careers\n"):
    respx.get("https://acme.example/robots.txt").respond(text=robots)
    respx.get("https://acme.example/").respond(
        html=HOME, headers={"server": "cloudflare", "cf-ray": "abc"})
    for path, title in [("/about-us", "About"), ("/products/", "Products"),
                        ("/contact", "Contact"), ("/careers", "Careers")]:
        respx.get(f"https://acme.example{path}").respond(html=PAGE.format(t=title))


def test_choose_pages_prefers_shallow_same_site_html_links():
    home = parse_page("https://acme.example/", "home", 200, HOME)
    chosen = dict((kind, url) for url, kind in choose_pages(home, max_extra=7))
    assert chosen["about"] == "https://acme.example/about-us"  # not /about-us/team/...
    assert chosen["products"] == "https://acme.example/products/"  # not the PDF
    assert chosen["careers"] == "https://acme.example/careers"
    assert all("other.example" not in u for u in chosen.values())


@respx.mock
def test_crawl_obeys_robots_and_collects_pages():
    _mock_site()
    with Crawler(delay=0) as c:
        result = c.crawl("acme.example")
    kinds = [p.kind for p in result.ok_pages]
    assert kinds[0] == "home" and "about" in kinds and "products" in kinds
    assert "careers" not in kinds
    assert any("robots.txt disallows" in n for n in result.notes)
    assert all(r.call_count <= 1 for r in respx.routes)  # each page fetched once


@respx.mock
def test_crawl_handles_blocked_site():
    respx.get("https://blocked.example/robots.txt").respond(404)
    respx.get("https://blocked.example/").respond(403)
    with Crawler(delay=0) as c:
        result = c.crawl("https://blocked.example/")
    assert result.ok_pages == []
    assert any("403" in n for n in result.notes)


def test_contacts_keep_role_emails_only():
    home = parse_page("https://acme.example/", "home", 200, HOME)
    emails, phones = extract_contacts([home])
    assert emails == ["info@acme.example"]
    assert "+442079460000" in phones


def test_social_links_skip_personal_profiles():
    home = parse_page("https://acme.example/", "home", 200, HOME)
    social = extract_social_links([home])
    assert social["linkedin"] == "https://www.linkedin.com/company/acme-widgets"
    assert social["x"] == "https://twitter.com/acmewidgets"


@respx.mock
def test_detect_tech():
    _mock_site()
    with Crawler(delay=0) as c:
        result = c.crawl("acme.example", max_pages=1)
    tech = detect_tech(result)
    assert {"WordPress", "Google Tag Manager", "Cloudflare"} <= set(tech)
    assert tech.count("Cloudflare") == 1


def test_verify_ownership():
    home = parse_page("https://acme.example/", "home", 200, HOME)
    conf, reasons = verify_ownership([home], company_number="01234567",
                                     company_name="ACME WIDGETS LIMITED", postcode="EC1A 1AA")
    assert conf == "high" and len(reasons) == 3
    conf, _ = verify_ownership([home], company_number="09999999",
                               company_name="ACME WIDGETS LIMITED", postcode=None)
    assert conf == "medium"
    conf, _ = verify_ownership([home], company_number=None, company_name="Zeta Ltd", postcode=None)
    assert conf == "low"


def test_names_and_candidate_domains():
    assert core_name("GREGGS PLC") == "greggs"
    assert core_name("The Example Group Holdings Ltd") == "example"
    domains = candidate_domains("ACME WIDGETS LIMITED")
    assert domains[:3] == ["acmewidgets.co.uk", "acmewidgets.com", "acmewidgets.uk"]
    assert "acme-widgets.co.uk" in domains


@respx.mock
def test_discovery_by_domain_guess(monkeypatch):
    monkeypatch.delenv("INFORMANT_CONTACT", raising=False)
    respx.get(url__regex=r"https://acmewidgets\.co\.uk/.*").mock(side_effect=httpx.ConnectError("dns"))
    respx.get("https://acmewidgets.com/robots.txt").respond(404)
    respx.get("https://acmewidgets.com/").respond(html=HOME)
    with Crawler(delay=0) as c:
        found = discover_website(c, company_name="ACME WIDGETS LIMITED",
                                 company_number="01234567", postcode="EC1A 1AA")
    assert found.url.rstrip("/") == "https://acmewidgets.com"
    assert found.via == "domain_guess" and found.confidence == "high"


# --- LLM: run the real SDK against a local fake server ----------------------

LLM_OUTPUT = {
    "summary": "Acme makes industrial widgets for UK factories.",
    "business_model": "B2B",
    "sectors": ["Engineering & Manufacturing", "Made Up Sector"],
    "tags": ["Widgets", "industrial", "widgets "],
    "target_markets": ["UK manufacturers"],
    "products": [
        {"name": "Widget Pro", "description": "A strong widget.", "category": "widgets",
         "source_page": "https://acme.example/products/"},
        {"name": "Widget Lite", "description": "A light widget.", "category": "widgets",
         "source_page": "https://made-up.example/nowhere"},
    ],
    "offices": [{"label": "Head office", "address": "1 High St", "locality": "London",
                 "country": "United Kingdom", "source_page": "https://acme.example/contact"}],
}


class _FakeAnthropic(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        type(self).requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
        payload = json.dumps({
            "id": "msg_test", "type": "message", "role": "assistant", "model": body["model"],
            "content": [{"type": "text", "text": json.dumps(LLM_OUTPUT)}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def _fake_server():
    _FakeAnthropic.requests = []
    server = HTTPServer(("127.0.0.1", 0), _FakeAnthropic)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = anthropic.Anthropic(api_key="test", base_url=f"http://127.0.0.1:{server.server_port}",
                                 max_retries=0)
    return server, client


def test_llm_extraction_request_and_cleanup():
    server, client = _fake_server()
    try:
        home = parse_page("https://acme.example/", "home", 200, HOME)
        result = extract_with_llm("ACME WIDGETS LIMITED", [home], client=client)
    finally:
        server.shutdown()

    sent = _FakeAnthropic.requests[0]
    body = sent["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in sent["headers"].get("anthropic-beta", "")
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "We make widgets" in body["messages"][0]["content"]

    data = result.data
    assert data is not None
    assert data.sectors == ["Engineering & Manufacturing"]  # invented sector dropped
    assert data.tags == ["industrial", "widgets"]  # normalised + deduped
    assert all(s in SECTORS for s in data.sectors)


def test_llm_without_credentials_is_skipped_not_fatal():
    client = anthropic.Anthropic(api_key="bad", base_url="http://127.0.0.1:9", max_retries=0)
    home = parse_page("https://acme.example/", "home", 200, HOME)
    result = extract_with_llm("Acme", [home], client=client)
    assert result.data is None and result.notes


@respx.mock
def test_research_website_end_to_end(monkeypatch):
    _mock_site()
    respx.route(host="127.0.0.1").pass_through()
    server, client = _fake_server()
    monkeypatch.setenv("INFORMANT_LLM", "claude")
    monkeypatch.setattr("informant.llm.backends.claude.anthropic.Anthropic", lambda: client)
    profile = CompanyProfile(
        query="01234567",
        identity=Identity(name="ACME WIDGETS LIMITED", company_number="01234567"),
        locations=[Location(kind="registered_office", postal_code="EC1A 1AA")],
    )
    try:
        with Crawler(delay=0) as c:
            research_website(profile, crawler=c, user_url="https://acme.example/")
    finally:
        server.shutdown()

    assert profile.identity.website == "https://acme.example/"
    assert profile.identity.description.startswith("Acme makes")
    assert profile.website.confidence == "high"
    assert profile.website.business_model == "B2B"
    assert profile.products[0].name == "Widget Pro"
    assert profile.products[0].sources[0].name == "llm_extraction"
    # One source per item: the page the model named, if we really crawled it...
    assert [x.url for x in profile.products[0].sources] == ["https://acme.example/products/"]
    # ...otherwise the website itself, never an invented page.
    assert [x.url for x in profile.products[1].sources] == ["https://acme.example/"]
    office = next(l for l in profile.locations if l.kind == "office")
    assert [x.url for x in office.sources] == ["https://acme.example/contact"]
    assert any(l.kind == "office" for l in profile.locations)
    assert "Engineering & Manufacturing" in profile.classification.sectors
    # Round trip through JSON still validates.
    CompanyProfile.model_validate_json(profile.to_json())


# --- Ollama backend ---------------------------------------------------------

from informant.llm import choose_backend, extract_with_ollama  # noqa: E402

OLLAMA = "http://localhost:11434/api/chat"
OLLAMA_SHOW = "http://localhost:11434/api/show"


def test_choose_backend(monkeypatch):
    monkeypatch.delenv("INFORMANT_LLM", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert choose_backend() == "ollama"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert choose_backend() == "claude"
    monkeypatch.setenv("INFORMANT_LLM", "ollama")
    assert choose_backend() == "ollama"


@respx.mock
def test_ollama_extraction(monkeypatch):
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    respx.post(OLLAMA_SHOW).respond(json={"capabilities": ["completion"]})
    route = respx.post(OLLAMA).respond(json={
        "message": {"role": "assistant", "content": json.dumps(LLM_OUTPUT)},
        "done": True, "done_reason": "stop",
    })
    home = parse_page("https://acme.example/", "home", 200, HOME)
    result = extract_with_ollama("ACME WIDGETS LIMITED", [home])

    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "qwen3:4b" and body["stream"] is False
    assert body["format"]["type"] == "object"  # JSON schema constrains the output
    assert body["options"]["num_ctx"] >= 8192
    assert body["messages"][0]["role"] == "system"
    assert "think" not in body  # non-thinking model: flag must not be sent
    assert result.model == "ollama/qwen3:4b"
    assert result.data.sectors == ["Engineering & Manufacturing"]
    assert any("took" in n for n in result.notes)


@respx.mock
def test_ollama_turns_off_thinking_when_supported(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:4b")
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    respx.post(OLLAMA_SHOW).respond(json={"capabilities": ["completion", "thinking"]})
    route = respx.post(OLLAMA).respond(json={
        "message": {"role": "assistant", "content": json.dumps(LLM_OUTPUT)}, "done_reason": "stop"})
    home = parse_page("https://acme.example/", "home", 200, HOME)
    result = extract_with_ollama("Acme", [home])
    assert json.loads(route.calls[0].request.content)["think"] is False
    assert result.model == "ollama/qwen3:4b"


@respx.mock
def test_ollama_failures_are_notes_not_crashes():
    home = parse_page("https://acme.example/", "home", 200, HOME)
    respx.post(OLLAMA_SHOW).respond(404)

    respx.post(OLLAMA).mock(side_effect=httpx.ConnectError("refused"))
    r = extract_with_ollama("Acme", [home])
    assert r.data is None and "start the Ollama app" in r.notes[-1]

    respx.post(OLLAMA).respond(404, json={"error": "model not found"})
    r = extract_with_ollama("Acme", [home])
    assert "ollama pull" in r.notes[-1]

    respx.post(OLLAMA).respond(json={"message": {"content": "{not json"}, "done_reason": "stop"})
    r = extract_with_ollama("Acme", [home])
    assert r.data is None and "didn't match" in r.notes[-1]

    respx.post(OLLAMA).respond(json={"message": {"content": ""}, "done_reason": "length"})
    r = extract_with_ollama("Acme", [home])
    assert "OLLAMA_NUM_CTX" in r.notes[-1]


@respx.mock
def test_copied_prompt_output_is_discarded():
    """Small local models sometimes echo the instructions; that must never be stored."""
    echoed = dict(LLM_OUTPUT,
                  summary="Write a summary of two or three plain sentences saying what the company does",
                  products=[{"name": "product name", "description": "one-sentence description",
                             "category": "short category", "source_page": "https://acme.example/"}])
    respx.post(OLLAMA_SHOW).respond(json={"capabilities": ["completion"]})
    respx.post(OLLAMA).respond(json={"message": {"content": json.dumps(echoed)}, "done_reason": "stop"})
    home = parse_page("https://acme.example/", "home", 200, HOME)
    r = extract_with_ollama("Acme", [home])
    assert r.data is None and "copied the prompt" in r.notes[-1]


def test_repeated_lines_sent_once():
    from informant.llm import build_page_text
    menu = "Home\nAbout us\nOur menu\nCareers\n"
    footer = "\n(c) Acme Ltd. Registered in England no. 01234567\nCookie settings"
    pages = [
        parse_page("https://acme.example/", "home", 200,
                   f"<html><body><pre>{menu}Welcome to Acme widgets.{footer}</pre></body></html>"),
        parse_page("https://acme.example/investors", "investors", 200,
                   f"<html><body><pre>{menu}Annual results 2026: revenue up 12%.{footer}</pre></body></html>"),
    ]
    text, notes = build_page_text(pages)
    assert text.count("Our menu") == 1 and text.count("Cookie settings") == 1
    assert "Welcome to Acme widgets." in text
    assert "Annual results 2026: revenue up 12%." in text  # each page's own content kept
    assert "=== PAGE (investors)" in text


def test_fair_shares():
    from informant.llm import fair_shares
    # Short pages take what they need; long ones split the rest equally.
    assert fair_shares([1_000, 50_000, 50_000], 21_000, cap=12_000) == [1_000, 10_000, 10_000]
    # Nothing exceeds the cap, even with spare budget.
    assert fair_shares([30_000], 60_000, cap=12_000) == [12_000]
    # Empty pages get nothing; total never exceeds the budget.
    shares = fair_shares([17_000, 0, 34_000, 14_000, 7_000, 4_000], 28_000, cap=12_000)
    assert shares[1] == 0 and sum(shares) <= 28_000 and all(s >= 4_000 for s in shares if s)


def test_long_page_cannot_crowd_out_investors():
    from informant.llm import build_page_text
    legal = parse_page("https://bank.example/info", "products", 200,
                       "<html><body><p>" + "Clause text. " * 20_000 + "</p></body></html>")
    inv = parse_page("https://bank.example/investors", "investors", 200,
                     "<html><body><p>Annual report: profit of 60 million pounds.</p></body></html>")
    text, _ = build_page_text([legal, inv], total_limit=20_000)
    assert "Annual report: profit of 60 million pounds." in text


def test_choose_pages_skips_legal_and_promo_links():
    html = """<html><body>
      <a href="/service-information">Our services</a>
      <a href="/terms-and-conditions">Products terms</a>
      <a href="/business-banking">Business account: an account packed with tools for your company</a>
      <a href="/investor-information">Investor information</a>
      <a href="/privacy-policy">About your privacy</a>
      <a href="/who-we-are">Who we are</a>
    </body></html>"""
    home = parse_page("https://bank.example/", "home", 200, html)
    chosen = dict((kind, url) for url, kind in choose_pages(home, max_extra=7))
    assert chosen == {"about": "https://bank.example/who-we-are",
                      "investors": "https://bank.example/investor-information"}


@respx.mock
def test_about_fallback_when_not_linked():
    respx.get("https://bank.example/robots.txt").respond(404)
    respx.get("https://bank.example/").respond(html=NORMAL_HOME)
    respx.get("https://bank.example/about").respond(404)
    respx.get("https://bank.example/about-us").respond(
        html="<html><head><title>About Bank</title></head><body><p>We are a bank.</p></body></html>")
    with Crawler(delay=0) as c:
        result = c.crawl("https://bank.example/")
    about = [p for p in result.ok_pages if p.kind == "about"]
    assert about and about[0].url.rstrip("/").endswith("/about-us")


NORMAL_HOME = ("<html><head><title>Bank</title></head><body><p>"
               + "Welcome to the bank. " * 50 + "</p></body></html>")


PLANLOGIC_LIKE = """<html><head><title>Planlogic</title></head><body><nav>
  <a href="http://www.planlogic.example">HOME</a>
  <a href="https://planlogic.example/eos/">EOS</a>
  <a href="https://planlogic.example/work-with-us/">WORK WITH US</a>
  <a href="https://planlogic.example/case-studies/">CASE STUDIES</a>
  <a href="https://planlogic.example/contact-us/">CONTACT US</a>
  <a href="https://planlogic.example/EOS">LEARN MORE</a>
  <a href="https://planlogic.example/contact-us/">About Planlogic</a>
  <a href="https://planlogic.example/fr/">Francais</a>
  <a href="https://planlogic.example/blog/2024/05/some-post/">A post</a>
  <a href="https://planlogic.example/my-account/">Log in</a>
  <a href="https://planlogic.example/cookie-policy/">Cookies</a>
</nav><p>Leaders in ETRM innovation.</p></body></html>"""


def test_short_link_labels_match():
    """Regression: label matching was silently broken by a stray control character."""
    home = parse_page("https://x.example/", "home", 200,
                      '<html><body><a href="/page-17">Investors</a></body></html>')
    assert choose_pages(home, 7) == [("https://x.example/page-17", "investors")]


def test_customers_kind_and_label_cannot_overrule_path():
    home = parse_page("https://planlogic.example/", "home", 200, PLANLOGIC_LIKE)
    chosen = dict((kind, url) for url, kind in choose_pages(home, 7))
    assert chosen["customers"] == "https://planlogic.example/case-studies/"
    assert chosen["contact"] == "https://planlogic.example/contact-us/"
    assert "about" not in chosen  # "About Planlogic" links to /contact-us: not an about page


def test_menu_links_fill_spare_slots():
    from informant.sources.website.pages import menu_links
    home = parse_page("https://planlogic.example/", "home", 200, PLANLOGIC_LIKE)
    taken = {"/work-with-us", "/case-studies", "/contact-us"}
    # /eos/ and /EOS are one page; language copies, deep posts, login and legal are skipped.
    assert menu_links(home, taken, 5) == ["https://planlogic.example/eos/"]


@respx.mock
def test_crawl_fills_slots_and_ignores_fake_404_about():
    respx.get("https://planlogic.example/robots.txt").respond(404)
    respx.get("https://planlogic.example/").respond(html=PLANLOGIC_LIKE)
    for path in ("/eos/", "/work-with-us/", "/case-studies/", "/contact-us/",
                 "/blog/2024/05/some-post/"):
        respx.get(f"https://planlogic.example{path}").respond(html=PAGE.format(t=path))
    # A site that answers 200 with a "Page not found" page must not count as an about page.
    respx.get("https://planlogic.example/about").respond(
        html="<html><head><title>Page not found</title></head><body><p>Sorry.</p></body></html>")
    respx.get("https://planlogic.example/about-us").respond(404)
    with Crawler(delay=0) as c:
        result = c.crawl("https://planlogic.example/")
    kinds = {p.kind: p.url for p in result.ok_pages}
    assert "about" not in kinds
    assert kinds["other"] == "https://planlogic.example/eos/"
    assert kinds["customers"] == "https://planlogic.example/case-studies/"
