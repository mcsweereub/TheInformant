"""Safety rails for every HTTP request made to addresses we don't control.

* One honest User-Agent for the whole program.
* No requests to private, loopback, link-local or other non-public addresses: website
  addresses come from Wikidata, guessed domains, web search, links in pages and
  redirects, and any of them could point at this machine (e.g. Ollama on 127.0.0.1) or
  the local network (a router at 192.168.1.1). The check runs on every request,
  including each step of a redirect chain. INFORMANT_NETGUARD=off turns it off (tests
  that mock fake hosts do this).
* Responses are read with a size cap, stopping once it is reached, so a huge or endless
  response can't fill memory.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from functools import lru_cache

import httpx

USER_AGENT = "Mozilla/5.0 (compatible; informant/0.1; personal company research)"
_OFF = {"0", "off", "false", "no"}


class BlockedAddress(httpx.RequestError):
    """The request was refused because the host is not a public internet address."""


class TooLarge(httpx.RequestError):
    """The response body went over the size cap."""


def guard_enabled() -> bool:
    return os.environ.get("INFORMANT_NETGUARD", "on").strip().lower() not in _OFF


def _public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    # is_global excludes private, loopback, link-local, carrier-grade NAT (100.64/10),
    # reserved and documentation ranges
    return ip.is_global and not ip.is_multicast


@lru_cache(maxsize=1024)
def _resolve(host: str) -> tuple[str, ...]:
    try:
        return tuple({info[4][0] for info in socket.getaddrinfo(host, None)})
    except (OSError, UnicodeError):
        return ()


def is_public_host(host: str | None) -> bool:
    """False when the host is, or resolves to, a non-public address. A name that
    doesn't resolve counts as public: the request will fail on its own."""
    host = (host or "").strip().strip("[]").rstrip(".").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        return False
    try:
        return _public_ip(ipaddress.ip_address(host))
    except ValueError:
        pass  # a name, not an address literal
    return all(_public_ip(ipaddress.ip_address(a.split("%")[0])) for a in _resolve(host))


def check_url(url: str | httpx.URL) -> None:
    """Raise BlockedAddress for a URL whose host isn't public (when the guard is on)."""
    if not guard_enabled():
        return
    u = httpx.URL(str(url))
    if u.scheme not in ("http", "https"):
        raise BlockedAddress(f"refused {u.scheme} address")
    if not is_public_host(u.host):
        raise BlockedAddress(f"refused non-public address {u.host}")


def _guard_hook(request: httpx.Request) -> None:
    try:
        check_url(request.url)
    except BlockedAddress as e:
        raise BlockedAddress(str(e), request=request) from None


def safe_client(*, timeout: float | httpx.Timeout = 30.0, headers: dict | None = None,
                follow_redirects: bool = True) -> httpx.Client:
    """An httpx client that identifies the program and refuses non-public addresses on
    every request, redirects included."""
    return httpx.Client(timeout=timeout, follow_redirects=follow_redirects,
                        headers={"User-Agent": USER_AGENT, **(headers or {})},
                        event_hooks={"request": [_guard_hook]})


def get_capped(http: httpx.Client, url: str, max_bytes: int, **kwargs) -> tuple[httpx.Response, bytes]:
    """GET a URL, reading at most `max_bytes` of the body. Returns the response (headers,
    status, final URL) and the bytes read. Raises TooLarge when the body is bigger (a
    Content-Length over the cap is refused before reading anything)."""
    with http.stream("GET", url, **kwargs) as resp:
        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise TooLarge(f"{url} is {int(declared):,} bytes (limit {max_bytes:,})", request=resp.request)
        chunks, size = [], 0
        for chunk in resp.iter_bytes(65536):
            size += len(chunk)
            if size > max_bytes:
                raise TooLarge(f"{url} is over {max_bytes:,} bytes", request=resp.request)
            chunks.append(chunk)
    return resp, b"".join(chunks)


def read_truncated(http: httpx.Client, url: str, max_bytes: int, **kwargs) -> tuple[httpx.Response, bytes]:
    """Like get_capped, but keeps the first `max_bytes` instead of failing (for web pages,
    where the start of the HTML is still useful)."""
    with http.stream("GET", url, **kwargs) as resp:
        chunks, size = [], 0
        for chunk in resp.iter_bytes(65536):
            chunks.append(chunk)
            size += len(chunk)
            if size >= max_bytes:
                break
    return resp, b"".join(chunks)[:max_bytes]


def decode(resp: httpx.Response, body: bytes) -> str:
    """Body text, as UTF-8 whenever the bytes are valid UTF-8 (servers often mislabel
    the charset), else the declared encoding."""
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as e:
        if e.start >= len(body) - 3:  # a character cut in half by the size cap
            try:
                return body[:e.start].decode("utf-8")
            except UnicodeDecodeError:
                pass
    try:
        return body.decode(resp.encoding or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")
