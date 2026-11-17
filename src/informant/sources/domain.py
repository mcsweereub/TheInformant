"""Signals from a company's web domain, all from public records:

  dns           MX, TXT and DMARC records, read through Cloudflare's DNS-over-HTTPS JSON
                API (the same answers any DNS lookup gives). They show the email provider,
                the services allowed to send its email (SPF) and the services the domain
                has been verified with (TXT verification records): a view of the tools a
                company uses that its website doesn't show.
  certificates  Certificate transparency logs, through SSLMate's Cert Spotter API (free,
                10 requests an hour without a key). Every public TLS certificate is
                logged, so they list subdomains (portals, products, sometimes a client's
                name) and other domains on the same certificates. crt.sh is not used:
                its robots.txt disallows automated access.
  wayback       The Internet Archive's CDX API: when the site was first and last archived.
                A domain can be older than the company that now owns it.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

import httpx

from ..schema import Domain, Source
from .fetchutil import api_client, cached_get
from .website.extract import site_host

DOH_URL = "https://cloudflare-dns.com/dns-query"
CERTSPOTTER_URL = "https://api.certspotter.com/v1/issuances"
WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
DNS_MAX_AGE = timedelta(days=1)
MAX_SUBDOMAINS = 150

# MX host fragment -> email provider
MX_PROVIDERS = [
    ("protection.outlook.com", "Microsoft 365"), ("outlook.com", "Microsoft 365"),
    ("google.com", "Google Workspace"), ("googlemail.com", "Google Workspace"),
    ("mimecast", "Mimecast"), ("pphosted.com", "Proofpoint"), ("ppe-hosted.com", "Proofpoint"),
    ("messagelabs.com", "Symantec Email Security"), ("barracudanetworks.com", "Barracuda"),
    ("zoho", "Zoho Mail"), ("secureserver.net", "GoDaddy"), ("ionos", "IONOS"),
    ("ovh.net", "OVH"), ("mailgun.org", "Mailgun"), ("awsapps.com", "Amazon WorkMail"),
    ("icloud.com", "iCloud Mail"), ("fastmail", "Fastmail"), ("sophos", "Sophos Email"),
    ("trendmicro", "Trend Micro Email Security"), ("forcepoint", "Forcepoint"),
]
# SPF include fragment -> service sending email for the domain
SPF_SERVICES = [
    ("spf.protection.outlook.com", "Microsoft 365"), ("_spf.google.com", "Google Workspace"),
    ("mcsv.net", "Mailchimp"), ("mandrillapp.com", "Mailchimp Transactional"),
    ("sendgrid.net", "SendGrid"), ("mailgun.org", "Mailgun"), ("amazonses.com", "Amazon SES"),
    ("_spf.salesforce.com", "Salesforce"), ("pardot.com", "Salesforce Pardot"),
    ("exacttarget.com", "Salesforce Marketing Cloud"), ("hubspotemail.net", "HubSpot"),
    ("hubspot", "HubSpot"), ("zendesk.com", "Zendesk"), ("freshdesk.com", "Freshdesk"),
    ("happyfox.com", "HappyFox"), ("helpscoutemail.com", "Help Scout"),
    ("intercom.io", "Intercom"), ("mktomail.com", "Marketo"), ("createsend.com", "Campaign Monitor"),
    ("sendinblue.com", "Brevo"), ("brevo.com", "Brevo"), ("mtasv.net", "Postmark"),
    ("sparkpostmail.com", "SparkPost"), ("zoho", "Zoho"), ("shopify.com", "Shopify"),
    ("klaviyo", "Klaviyo"), ("service-now.com", "ServiceNow"), ("atlassian.net", "Atlassian"),
    ("netsuite.com", "NetSuite"), ("qualtrics.com", "Qualtrics"), ("docusign", "DocuSign"),
    ("xero.com", "Xero"), ("mimecast", "Mimecast"), ("pphosted.com", "Proofpoint"),
    ("messagelabs.com", "Symantec Email Security"), ("smtp2go.com", "SMTP2GO"),
    ("sage.com", "Sage"), ("workday.com", "Workday"), ("greenhouse.io", "Greenhouse"),
]
# TXT record prefix -> service the domain is verified with
TXT_VERIFICATIONS = [
    ("ms=", "Microsoft 365"), ("google-site-verification=", "Google Search Console"),
    ("atlassian-domain-verification=", "Atlassian"), ("docusign=", "DocuSign"),
    ("facebook-domain-verification=", "Meta Business"), ("apple-domain-verification=", "Apple"),
    ("adobe-idp-site-verification=", "Adobe"), ("adobe-sign-verification=", "Adobe Sign"),
    ("zoom_verify_", "Zoom"), ("stripe-verification=", "Stripe"),
    ("slack-domain-verification=", "Slack"), ("dropbox-domain-verification=", "Dropbox"),
    ("miro-verification=", "Miro"), ("hubspot-developer-verification=", "HubSpot"),
    ("onetrust-domain-verification=", "OneTrust"), ("cisco-ci-domain-verification=", "Cisco Webex"),
    ("webexdomainverification", "Cisco Webex"), ("globalsign-domain-verification=", "GlobalSign"),
    ("knowbe4-site-verification=", "KnowBe4"), ("mongodb-site-verification=", "MongoDB Atlas"),
    ("openai-domain-verification=", "OpenAI"), ("anthropic-domain-verification", "Anthropic"),
    ("canva-site-verification=", "Canva"), ("airtable-verification=", "Airtable"),
    ("loom-site-verification=", "Loom"), ("postman-domain-verification=", "Postman"),
    ("status-page-domain-verification=", "Atlassian Statuspage"),
    ("workplace-domain-verification=", "Meta Workplace"), ("teamviewer-sso-verification=", "TeamViewer"),
    ("zapier-domain-verification-challenge=", "Zapier"), ("citrix-verification-code=", "Citrix"),
    ("segment-site-verification=", "Segment"), ("klaviyo-site-verification=", "Klaviyo"),
    ("smartsheet-site-validation=", "Smartsheet"), ("box-domain-verification=", "Box"),
    ("lucid-verification=", "Lucid"), ("logmein-verification-code=", "LogMeIn"),
    ("wrike-verification=", "Wrike"), ("have-i-been-pwned-verification=", "Have I Been Pwned"),
    ("figma-domain-verification=", "Figma"), ("notion-domain-verification=", "Notion"),
    ("asana-verification=", "Asana"), ("monday-verification=", "monday.com"),
    ("brevo-code:", "Brevo"), ("mailchimp-verification", "Mailchimp"),
    ("yandex-verification:", "Yandex"), ("ahrefs-site-verification_", "Ahrefs"),
    ("sophos-domain-verification=", "Sophos"), ("intercom-domain-validation=", "Intercom"),
    ("pardot", "Salesforce Pardot"), ("salesforce", "Salesforce"), ("zendesk", "Zendesk"),
    ("_github-challenge", "GitHub"), ("gitlab-pages-verification-code=", "GitLab"),
    ("protonmail-verification=", "Proton Mail"), ("tiktok-developers-site-verification=", "TikTok"),
    ("pinterest-site-verification=", "Pinterest"), ("bugcrowd-verification=", "Bugcrowd"),
    ("hackerone", "HackerOne"), ("1password-site-verification=", "1Password"),
    ("okta-verification", "Okta"), ("duo_sso_verification", "Duo"),
]
_SECOND_LEVEL = {"co", "org", "ac", "gov", "ltd", "plc", "me", "net", "sch", "nhs", "com"}


def registrable(host: str) -> str:
    """'a.b.example.co.uk' -> 'example.co.uk'; 'api.monzo.com' -> 'monzo.com'."""
    parts = host.lower().strip(".").split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _match(value: str, table: list[tuple[str, str]], prefix: bool = False) -> list[str]:
    v = value.lower()
    return [name for frag, name in table if (v.startswith(frag) if prefix else frag in v)]


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


class DomainLookup:
    def __init__(self, cache=None, http: httpx.Client | None = None) -> None:
        self.cache = cache
        self.http = http or api_client()
        # Cloudflare answers in JSON only when asked for it in the Accept header.
        self.dns_http = http or api_client(headers={"Accept": "application/dns-json"})

    def close(self) -> None:
        self.http.close()
        self.dns_http.close()

    def dns(self, name: str, rtype: str) -> list[str]:
        """The records' values (TXT strings joined, quotes removed)."""
        params = {"name": name, "type": rtype}
        data = json.loads(cached_get(self.dns_http, self.cache, DOH_URL, params, max_age=DNS_MAX_AGE))
        return [a.get("data", "").replace('" "', "").strip('"') for a in data.get("Answer") or []
                if a.get("type") == {"MX": 15, "TXT": 16}[rtype]]


def read_dns(lookup: DomainLookup, domain: str, section: Domain) -> None:
    mx = lookup.dns(domain, "MX")
    txt = lookup.dns(domain, "TXT")
    dmarc = lookup.dns(f"_dmarc.{domain}", "TXT")
    providers = _unique([p for record in mx for p in _match(record.split()[-1], MX_PROVIDERS)])
    section.email_provider = providers[0] if providers else None
    spf = next((t for t in txt if t.lower().startswith("v=spf1")), None)
    if spf:
        includes = re.findall(r"(?:include|redirect)[:=](\S+)", spf, re.I)
        section.email_services = _unique([s for inc in includes for s in _match(inc, SPF_SERVICES)])
        policy = {"-all": "strict (-all)", "~all": "soft fail (~all)", "?all": "neutral (?all)",
                  "+all": "allows anyone (+all)"}
        ending = next((v for k, v in policy.items() if spf.lower().rstrip().endswith(k)), None)
        if ending:
            section.email_security.append(f"SPF: {ending}")
    section.verified_services = _unique([s for t in txt for s in _match(t, TXT_VERIFICATIONS, prefix=True)])
    record = next((d for d in dmarc if d.lower().startswith("v=dmarc1")), None)
    if record:
        policy = re.search(r"\bp=(\w+)", record, re.I)
        section.email_security.append(f"DMARC policy: {policy.group(1).lower() if policy else 'unset'}")
    elif mx:
        section.email_security.append("no DMARC record (its email is easier to spoof)")
    section.sources.append(Source(name="dns", url=f"{DOH_URL}?name={domain}"))


def read_certificates(lookup: DomainLookup, domain: str, section: Domain) -> None:
    params = [("domain", domain), ("include_subdomains", "true"), ("expand", "dns_names")]
    url = str(httpx.URL(CERTSPOTTER_URL, params=params))
    data = json.loads(cached_get(lookup.http, lookup.cache, url, max_age=timedelta(days=7)))
    names = {n.lower().lstrip("*.") for cert in data for n in cert.get("dns_names") or []}
    section.subdomains = sorted(n for n in names if n.endswith("." + domain))[:MAX_SUBDOMAINS]
    others = {registrable(n) for n in names if not (n == domain or n.endswith("." + domain))}
    section.other_domains = sorted(others - {domain})
    if len(data) >= 100:
        section.notes.append("only the first 100 current certificates were read")
    section.sources.append(Source(name="certificates", url="https://sslmate.com/ct_search_api/"))


def read_wayback(lookup: DomainLookup, domain: str, section: Domain) -> None:
    def one(limit: int) -> date | None:
        params = {"url": domain, "output": "json", "fl": "timestamp", "limit": str(limit)}
        if limit < 0:
            params["fastLatest"] = "true"
        rows = json.loads(cached_get(lookup.http, lookup.cache, WAYBACK_CDX, params,
                                     max_age=timedelta(days=7)) or "[]")
        stamps = [r[0] for r in rows[1:] if r and r[0].isdigit()]
        return datetime.strptime(stamps[0][:8], "%Y%m%d").date() if stamps else None

    section.first_archived = one(1)
    section.last_archived = one(-1) if section.first_archived else None
    section.sources.append(Source(name="wayback", url=f"https://web.archive.org/web/*/{domain}"))


def fetch_domain_signals(website: str, *, incorporated_on: date | None = None, cache=None,
                         http: httpx.Client | None = None) -> Domain:
    """DNS, certificate and archive signals for the website's domain. Each part that
    fails leaves a note; the others still run."""
    domain = registrable(site_host(website))
    section = Domain(name=domain)
    own = http is None
    lookup = DomainLookup(cache=cache, http=http)
    try:
        for label, step in (("DNS", read_dns), ("certificate logs", read_certificates),
                            ("Wayback Machine", read_wayback)):
            try:
                step(lookup, domain, section)
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                why = "rate limited, try again later" if code == 429 else f"HTTP {code}"
                section.notes.append(f"{label} lookup failed ({why})")
            except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
                section.notes.append(f"{label} lookup failed: {type(e).__name__}")
    finally:
        if own:
            lookup.close()
    if incorporated_on and section.first_archived and section.first_archived < incorporated_on - timedelta(days=365):
        section.notes.append(
            f"the site was archived from {section.first_archived:%Y}, before the company was "
            f"incorporated in {incorporated_on:%Y}: the domain may have had an earlier owner, "
            "or belonged to a predecessor business")
    return section
