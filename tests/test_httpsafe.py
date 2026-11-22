"""Safety rails: no requests to private addresses, capped downloads, bot-challenge pages,
and API keys kept out of the cache."""

import gzip

import httpx
import pytest
import respx

from informant.cache import PageCache
from informant.sources import httpsafe
from informant.sources.fetchutil import cached_get
from informant.sources.website.fetch import Crawler

PUBLIC_IP = "93.184.216.34"


@pytest.fixture
def guard_on(monkeypatch):
    """Guard switched on, with made-up names "resolving" to a public address (no DNS)."""
    monkeypatch.setenv("INFORMANT_NETGUARD", "on")
    monkeypatch.setattr(httpsafe, "_resolve", lambda host: (
        ("192.168.0.10",) if host == "intranet.example" else (PUBLIC_IP,)))


@pytest.mark.parametrize("host", [
    "127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.4.4", "169.254.169.254", "100.64.1.1",
    "0.0.0.0", "::1", "::ffff:127.0.0.1", "fe80::1", "localhost", "printer.local",
    "router.lan", "intranet.example", ""])
def test_non_public_hosts_are_refused(guard_on, host):
    assert not httpsafe.is_public_host(host)


@pytest.mark.parametrize("host", ["8.8.8.8", "2606:4700:4700::1111", "www.greggs.com"])
def test_public_hosts_are_allowed(guard_on, host):
    assert httpsafe.is_public_host(host)


@respx.mock
def test_a_redirect_to_this_machine_is_refused(guard_on):
    respx.get("https://www.acme.example/").respond(301, headers={"Location": "http://127.0.0.1:11434/api/tags"})
    local = respx.get("http://127.0.0.1:11434/api/tags").respond(200, text="secret")
    with httpsafe.safe_client() as http, pytest.raises(httpsafe.BlockedAddress):
        http.get("https://www.acme.example/")
    assert not local.called


@respx.mock
def test_the_crawler_notes_and_skips_private_addresses(guard_on):
    respx.get("http://192.168.1.1/robots.txt").respond(404)
    router = respx.get("http://192.168.1.1/").respond(200, html="<html>admin</html>")
    notes: list[str] = []
    with Crawler(browser="never", sitemap=False, delay=0) as c:
        assert c.fetch("http://192.168.1.1/", "home", notes) is None
    assert not router.called and "non-public" in " ".join(notes)


def test_the_guard_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("INFORMANT_NETGUARD", "off")
    httpsafe.check_url("http://127.0.0.1/")  # no error


def test_non_web_schemes_are_refused(guard_on):
    with pytest.raises(httpsafe.BlockedAddress):
        httpsafe.check_url("ftp://files.example/report.pdf")


@respx.mock
def test_downloads_stop_at_the_cap():
    respx.get("https://files.example/big.pdf").respond(200, content=b"%PDF" + b"x" * 5000)
    respx.get("https://files.example/declared.pdf").respond(
        200, content=b"%PDF", headers={"Content-Length": "999999999"})
    with httpx.Client() as http:
        with pytest.raises(httpsafe.TooLarge):
            httpsafe.get_capped(http, "https://files.example/big.pdf", 1000)
        with pytest.raises(httpsafe.TooLarge):  # refused from the header, before reading
            httpsafe.get_capped(http, "https://files.example/declared.pdf", 1000)
        resp, body = httpsafe.get_capped(http, "https://files.example/big.pdf", 10_000)
        assert resp.status_code == 200 and len(body) == 5004
        resp, body = httpsafe.read_truncated(http, "https://files.example/big.pdf", 1000)
        assert len(body) == 1000  # pages keep their first part


def test_a_character_cut_by_the_cap_is_dropped():
    resp = httpx.Response(200, headers={"Content-Type": "text/html; charset=iso-8859-1"})
    body = "price £5".encode()[:-2] + "£".encode()[:1]  # "price £" with the £ cut in half
    assert httpsafe.decode(resp, "café £".encode()[:-1]) == "café "
    assert httpsafe.decode(resp, body).startswith("price")


@respx.mock
def test_bot_protection_pages_are_not_read_as_content():
    respx.get("https://www.shop.example/robots.txt").respond(404)
    respx.get("https://www.shop.example/").respond(200, html=(
        "<html><head><title>Pardon Our Interruption</title></head><body>"
        "<p>As you were browsing something about your browser made us think you were a bot.</p>"
        "</body></html>"))
    notes: list[str] = []
    with Crawler(browser="never", sitemap=False, delay=0) as c:
        page = c.fetch("https://www.shop.example/", "home", notes)
    assert page.status_code == 403 and not page.text
    assert "bot-protection" in notes[0]


@respx.mock
def test_a_long_page_mentioning_captcha_is_kept():
    respx.get("https://www.ok.example/robots.txt").respond(404)
    words = " ".join(f"word{i}" for i in range(600))
    respx.get("https://www.ok.example/").respond(200, html=(
        f"<html><body><p>{words}</p><form>Are you a robot? This form uses reCAPTCHA.</form></body></html>"))
    with Crawler(browser="never", sitemap=False, delay=0) as c:
        page = c.fetch("https://www.ok.example/", "home", [])
    assert page.status_code == 200 and "word5" in page.text


@respx.mock
def test_api_keys_are_sent_but_not_cached(tmp_path):
    route = respx.get("https://content.example/search").respond(200, text='{"ok": 1}')
    cache = PageCache(tmp_path)
    with httpx.Client() as http:
        cached_get(http, cache, "https://content.example/search", {"q": "acme"},
                   secret_params={"api-key": "SECRET123"})
        assert route.calls[0].request.url.params["api-key"] == "SECRET123"
        # reused from the cache under the URL without the key
        cached_get(http, cache, "https://content.example/search", {"q": "acme"},
                   secret_params={"api-key": "SECRET123"})
    assert route.call_count == 1
    stored = [gzip.open(f, "rt", encoding="utf-8").read() for f in (tmp_path / "pages").glob("*.gz")]
    assert stored and not any("SECRET123" in s for s in stored)
