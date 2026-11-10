"""Deterministic (non-LLM) extraction from crawled pages."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

from .crawler import CrawlResult, Page

# --- contacts -------------------------------------------------------------

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# first.last@ / first_last@ style local parts are almost always a named person;
# we keep only role-style addresses (info@, sales@, press@ ...).
PERSONAL_LOCAL_RE = re.compile(r"^[a-z]+[._-][a-z]+$", re.I)
UK_PHONE_RE = re.compile(r"(?:\+44\s?\(?0?\)?\s?|\b0)(?:\d[\s-]?){9,10}\b")
_ASSET_EXT = re.compile(r"\.(png|jpe?g|gif|svg|webp|css|js)$", re.I)


def _clean_email(raw: str) -> str | None:
    email = unquote(raw).split("?")[0].strip().strip(".").lower()
    if not EMAIL_RE.fullmatch(email) or _ASSET_EXT.search(email):
        return None
    local = email.split("@")[0]
    if PERSONAL_LOCAL_RE.match(local):
        return None
    return email


def _clean_phone(raw: str) -> str | None:
    digits = re.sub(r"[^\d+]", "", unquote(raw))
    if digits.startswith("+440"):
        digits = "+44" + digits[4:]
    n = len(digits.lstrip("+"))
    return digits if 10 <= n <= 13 else None


def extract_contacts(pages: list[Page]) -> tuple[list[str], list[str]]:
    emails: list[str] = []
    phones: list[str] = []
    for page in pages:
        for href, _ in page.links:
            if href.lower().startswith("mailto:") and (e := _clean_email(href[7:])):
                emails.append(e)
            elif href.lower().startswith("tel:") and (p := _clean_phone(href[4:])):
                phones.append(p)
        for m in EMAIL_RE.findall(page.text):
            if e := _clean_email(m):
                emails.append(e)
        if page.kind in ("contact", "home"):
            for m in UK_PHONE_RE.findall(page.text):
                if p := _clean_phone(m):
                    phones.append(p)
    return sorted(set(emails)), list(dict.fromkeys(phones))[:10]


# --- social links (company accounts only) ---------------------------------

SOCIAL_PATTERNS: dict[str, re.Pattern[str]] = {
    # LinkedIn: company/school pages only, never /in/ personal profiles.
    "linkedin": re.compile(r"^https?://([a-z]{2,3}\.)?linkedin\.com/(company|school|showcase)/[^/?#]+", re.I),
    "x": re.compile(r"^https?://(www\.)?(twitter|x)\.com/(?!intent|share|home)[A-Za-z0-9_]{1,15}/?$", re.I),
    "facebook": re.compile(r"^https?://([a-z]+\.)?facebook\.com/(?!sharer|share|dialog)[^/?#]+/?$", re.I),
    "instagram": re.compile(r"^https?://(www\.)?instagram\.com/[A-Za-z0-9_.]+/?$", re.I),
    "youtube": re.compile(r"^https?://(www\.)?youtube\.com/(@|c/|channel/|user/)[^/?#]+", re.I),
    "tiktok": re.compile(r"^https?://(www\.)?tiktok\.com/@[^/?#]+", re.I),
    "github": re.compile(r"^https?://(www\.)?github\.com/[A-Za-z0-9-]+/?$", re.I),
}


def extract_social_links(pages: list[Page]) -> dict[str, str]:
    found: dict[str, str] = {}
    for page in pages:
        for href, _ in page.links:
            for name, pattern in SOCIAL_PATTERNS.items():
                if name not in found and (m := pattern.match(href)):
                    found[name] = m.group(0).rstrip("/")
    return found


# --- tech stack -----------------------------------------------------------

# (name, where, regex) - where is "html" or a response header name.
TECH_SIGNATURES: list[tuple[str, str, str]] = [
    ("WordPress", "html", r"wp-content|wp-includes"),
    ("WooCommerce", "html", r"woocommerce"),
    ("Shopify", "html", r"cdn\.shopify\.com|Shopify\.theme"),
    ("Wix", "html", r"static\.wixstatic\.com|wix\.com"),
    ("Squarespace", "html", r"squarespace\.com|static1\.squarespace"),
    ("Webflow", "html", r"webflow\.(com|js)|data-wf-site"),
    ("Drupal", "html", r"Drupal\.settings|/sites/default/files"),
    ("Joomla", "html", r"/media/jui/|Joomla!"),
    ("HubSpot CMS", "html", r"hs-sites\.com|hubspot\.net/hub"),
    ("HubSpot", "html", r"js\.hs-scripts\.com|js\.hsforms\.net"),
    ("Magento", "html", r"Magento_|mage/cookies"),
    ("Next.js", "html", r"__NEXT_DATA__|/_next/static"),
    ("Nuxt", "html", r"__NUXT__|/_nuxt/"),
    ("Gatsby", "html", r"___gatsby"),
    ("React", "html", r"data-reactroot|react-dom"),
    ("Angular", "html", r"ng-version="),
    ("Vue.js", "html", r"data-v-[0-9a-f]{6,}|vue(\.min)?\.js"),
    ("jQuery", "html", r"jquery(\.min)?\.js"),
    ("Bootstrap", "html", r"bootstrap(\.min)?\.(css|js)"),
    ("Tailwind CSS", "html", r"tailwind"),
    ("Google Analytics", "html", r"google-analytics\.com|gtag\(|googletagmanager\.com/gtag"),
    ("Google Tag Manager", "html", r"googletagmanager\.com/gtm\.js|GTM-[A-Z0-9]+"),
    ("Meta Pixel", "html", r"connect\.facebook\.net/.+/fbevents\.js"),
    ("Hotjar", "html", r"static\.hotjar\.com"),
    ("Intercom", "html", r"widget\.intercom\.io|intercomcdn"),
    ("Zendesk", "html", r"zdassets\.com|zendesk"),
    ("Drift", "html", r"js\.driftt\.com"),
    ("Salesforce", "html", r"force\.com|salesforce"),
    ("Marketo", "html", r"munchkin\.marketo\.net|mktoForms"),
    ("Stripe", "html", r"js\.stripe\.com"),
    ("OneTrust", "html", r"cdn\.cookielaw\.org|onetrust"),
    ("Cookiebot", "html", r"consent\.cookiebot\.com"),
    ("Greenhouse", "html", r"boards\.greenhouse\.io|greenhouse\.io/embed"),
    ("Lever", "html", r"jobs\.lever\.co"),
    ("Workable", "html", r"apply\.workable\.com"),
    ("Cloudflare", "server", r"cloudflare"),
    ("Cloudflare", "cf-ray", r"."),
    ("Nginx", "server", r"nginx"),
    ("Apache", "server", r"apache"),
    ("Microsoft IIS", "server", r"microsoft-iis"),
    ("Vercel", "server", r"vercel"),
    ("Netlify", "server", r"netlify"),
    ("Amazon CloudFront", "via", r"cloudfront"),
    ("Akamai", "server", r"akamai"),
    ("Fastly", "x-served-by", r"cache-"),
    ("PHP", "x-powered-by", r"php"),
    ("ASP.NET", "x-powered-by", r"asp\.net"),
    ("Express", "x-powered-by", r"express"),
]


def detect_tech(result: CrawlResult) -> list[str]:
    html = "\n".join(p.html for p in result.ok_pages)
    headers = {k.lower(): v for k, v in result.headers.items()}
    found: list[str] = []
    for name, where, pattern in TECH_SIGNATURES:
        if name in found:
            continue
        target = html if where == "html" else headers.get(where, "")
        if target and re.search(pattern, target, re.I):
            found.append(name)
    return found


# --- ownership verification -----------------------------------------------


def verify_ownership(
    pages: list[Page],
    *,
    company_number: str | None,
    company_name: str | None,
    postcode: str | None,
) -> tuple[str, list[str]]:
    """How confident are we that this site belongs to the company?

    UK companies must show their registered number on their website, so a
    match on that is strong evidence. Returns (confidence, reasons).
    """
    text = "\n".join(p.text for p in pages)
    # JavaScript-rendered sites often keep footer text in embedded JSON, so the
    # hard identifiers are also searched for in the raw HTML.
    raw = text + "\n" + "\n".join(p.html for p in pages)
    reasons: list[str] = []

    if company_number:
        variants = {company_number, company_number.lstrip("0")}
        if any(len(v) >= 6 and re.search(rf"(?<!\d){re.escape(v)}(?!\d)", raw) for v in variants):
            reasons.append(f"company number {company_number} appears on site")
    if postcode:
        pc = re.sub(r"\s+", "", postcode).upper()
        if pc and pc in re.sub(r"\s+", "", raw).upper():
            reasons.append(f"registered office postcode {postcode} appears on site")
    if company_name:
        core = core_name(company_name)
        titles = " ".join(p.title or "" for p in pages).lower()
        if core and core in titles:
            reasons.append(f"name '{core}' in page title")
        elif core and core in text.lower():
            reasons.append(f"name '{core}' appears in page text")

    strong = any(r.startswith(("company number", "registered office")) for r in reasons)
    if strong:
        return "high", reasons
    if any("page title" in r for r in reasons):
        return "medium", reasons
    return "low", reasons


_SUFFIXES = re.compile(
    r"\b(limited|ltd|plc|llp|lp|inc|group|holdings?|uk|international|"
    r"services|company|co|the|and|&)\b\.?",
    re.I,
)


def core_name(company_name: str) -> str:
    """'GREGGS PLC' -> 'greggs'; 'The Example Group Holdings Ltd' -> 'example'."""
    name = _SUFFIXES.sub(" ", company_name.lower())
    name = re.sub(r"[^a-z0-9 ]+", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def site_host(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host
