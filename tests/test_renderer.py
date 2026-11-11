"""Headless-browser fallback, tested with a fake renderer (no real Chromium)."""

import respx

from informant.crawler import Crawler, looks_js_rendered, parse_page
from informant.renderer import BrowserUnavailable

SHELL = ("<html><head><title>App</title>" + "<script src='/a.js'></script>" * 5 +
         "</head><body><div id='root'></div><noscript>Enable JavaScript</noscript></body></html>")
RICH = ("<html><head><title>Acme Bank</title></head><body>"
        "<nav><a href='/about'>About us</a> <a href='/products'>Products</a></nav>"
        "<p>" + "Acme Bank offers current accounts and savings to people across the UK. " * 20 +
        "</p></body></html>")
NORMAL = ("<html><head><title>Plain</title></head><body><p>" + "Plain old HTML site. " * 60 +
          "</p><a href='/about'>About</a></body></html>")


class FakeRenderer:
    def __init__(self, pages=None, unavailable=False):
        self.pages = pages or {}
        self.unavailable = unavailable
        self.calls = []
        self.closed = False

    def render(self, url):
        self.calls.append(url)
        if self.unavailable:
            raise BrowserUnavailable("Chromium isn't installed; install it with: uv run playwright install chromium")
        return url, 200, self.pages.get(url, RICH)

    def close(self):
        self.closed = True


def _site(host, home_html, others=()):
    respx.get(f"https://{host}/robots.txt").respond(404)
    respx.get(f"https://{host}/").respond(html=home_html)
    for path in others:
        respx.get(f"https://{host}{path}").respond(html=home_html)


def test_looks_js_rendered():
    assert looks_js_rendered(parse_page("https://x/", "home", 200, SHELL))
    assert not looks_js_rendered(parse_page("https://x/", "home", 200, NORMAL))


@respx.mock
def test_js_site_is_rendered_and_rest_of_site_goes_straight_to_browser():
    _site("bank.example", SHELL, others=["/about", "/products"])
    fake = FakeRenderer()
    with Crawler(delay=0, renderer=fake) as c:
        result = c.crawl("https://bank.example/", max_pages=3)
    pages = result.ok_pages
    assert all(p.rendered for p in pages) and len(pages) == 3
    assert "Acme Bank offers" in pages[0].text
    assert {p.kind for p in pages} == {"home", "about", "products"}  # links found in rendered nav
    # Subpages were rendered directly, not fetched over plain HTTP first.
    assert not respx.calls.last.request.url.path.startswith("/about")
    assert any("headless browser" in n for n in result.notes)
    assert fake.closed


@respx.mock
def test_normal_site_never_touches_browser():
    _site("plain.example", NORMAL, others=["/about"])
    fake = FakeRenderer()
    with Crawler(delay=0, renderer=fake) as c:
        result = c.crawl("https://plain.example/")
    assert fake.calls == []
    assert not any(p.rendered for p in result.pages)


@respx.mock
def test_browser_unavailable_falls_back_with_install_hint():
    _site("bank.example", SHELL)
    fake = FakeRenderer(unavailable=True)
    with Crawler(delay=0, renderer=fake) as c:
        result = c.crawl("https://bank.example/", max_pages=1)
    assert result.ok_pages and not result.ok_pages[0].rendered
    assert any("playwright install chromium" in n for n in result.notes)
    assert any("content may be missing" in n for n in result.notes)


@respx.mock
def test_never_mode_and_always_mode():
    _site("bank.example", SHELL)
    fake = FakeRenderer()
    with Crawler(delay=0, renderer=fake, browser="never") as c:
        result = c.crawl("https://bank.example/", max_pages=1)
    assert fake.calls == [] and any("--browser never" in n for n in result.notes)

    _site("plain.example", NORMAL)
    fake = FakeRenderer()
    with Crawler(delay=0, renderer=fake, browser="always") as c:
        result = c.crawl("https://plain.example/", max_pages=1)
    assert fake.calls == ["https://plain.example/"] and result.ok_pages[0].rendered


@respx.mock
def test_rendered_result_kept_only_if_it_shows_more():
    _site("bank.example", SHELL)
    fake = FakeRenderer(pages={"https://bank.example/": SHELL})  # browser sees nothing extra
    with Crawler(delay=0, renderer=fake) as c:
        result = c.crawl("https://bank.example/", max_pages=1)
    assert not result.ok_pages[0].rendered


@respx.mock
def test_robots_still_applies_to_browser():
    respx.get("https://bank.example/robots.txt").respond(text="User-agent: *\nDisallow: /\n")
    fake = FakeRenderer()
    with Crawler(delay=0, renderer=fake, browser="always") as c:
        result = c.crawl("https://bank.example/")
    assert fake.calls == [] and any("robots.txt disallows" in n for n in result.notes)


@respx.mock
def test_discovery_probe_never_renders():
    _site("bank.example", SHELL)
    fake = FakeRenderer()
    with Crawler(delay=0, renderer=fake, browser="always") as c:
        c.probe("bank.example")
    assert fake.calls == []
