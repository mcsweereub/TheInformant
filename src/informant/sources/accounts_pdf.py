"""Text of a company's accounts when Companies House has no machine-readable copy.

Large companies (groups, PLCs, banks) file accounts that Companies House stores as
scanned images, so there are no tagged figures to read. Two ways to get the text:

  1. The company's own annual report: a text-based PDF on its website or investor
     site. Found through Wikipedia references, crawled pages, investor and
     annual-report pages one link away, and the sitemap. A candidate is accepted
     only if its text names the company and the exact accounts date.
  2. OCR of the Companies House scan (needs Tesseract installed). A fast, reduced-size
     pass over every page finds the financial statements; only those pages are then
     read at higher resolution.

Either way, only the pages that matter are kept (statements, employees, principal
activities, auditor, ultimate controlling party, substantial shareholdings), ready
for the LLM step in llm/accounts.py.
"""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urljoin, urlparse

import httpx

from .companies_house.client import CompaniesHouseClient, CompaniesHouseError
from .companies_house.mapping import filing_document_url
from .website.extract import core_name, site_host

PDF = "application/pdf"
IXBRL = "application/xhtml+xml"
MAX_PDF_BYTES = 100_000_000  # annual reports with photos run to 60 MB (Monzo 2026: 59 MB)
MAX_CANDIDATES = 3  # annual-report PDFs downloaded and checked per company
MAX_HOPS = 6  # annual-report, investor and corporate pages followed to find the PDF
QUICK_OCR_WIDTH = 1200  # pixels: enough to find headings
FULL_OCR_WIDTH = 2000  # pixels: for the pages whose figures are read

_PDF_LINK = re.compile(r"\.pdf(\?|#|$)", re.I)
_REPORT_WORDS = re.compile(
    r"annual[\s_-]*report|report[\s_-]*(and|&)[\s_-]*accounts|annual[\s_-]*accounts|"
    r"financial[\s_-]*statements|(^|[^a-z])ar[\s_-]?(19|20)?\d\d([^0-9]|$)", re.I)
_NOT_REPORT = re.compile(
    r"pillar[\s_-]*3|pay[\s_-]*gap|modern[\s_-]*slavery|interim|half[\s_-]*year|h1|q[1-4]|"
    r"allergen|nutrition|sustainability|esg|tcfd|presentation|slides|notice[\s_-]*of|"
    r"agm|proxy|policy|terms|gender|tax[\s_-]*strategy|factsheet|brochure|menu", re.I)
_INVESTOR_PAGE = re.compile(r"investor|corporate|annual[\s_-]*report|results|reports|financial|plc",
                            re.I)
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"]


@dataclass
class PdfPage:
    number: int  # 1-based page in the document
    text: str
    topics: list[str] = field(default_factory=list)  # why it was chosen


@dataclass
class AccountsDocument:
    method: str  # "annual_report" (text PDF) or "ocr" (scanned Companies House filing)
    url: str
    made_up: date | None
    filed_on: str | None
    total_pages: int
    pages: list[PdfPage]
    notes: list[str] = field(default_factory=list)


# --- the latest accounts filing ------------------------------------------------------


def latest_accounts_filing(client: CompaniesHouseClient, number: str) -> dict | None:
    for item in client.filing_history(number, limit=12, category="accounts"):
        if (item.get("description") or "").startswith("accounts-with") and \
                (item.get("links") or {}).get("document_metadata"):
            return item
    return None


def made_up_date(item: dict) -> date | None:
    value = (item.get("description_values") or {}).get("made_up_date") or ""
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def date_forms(d: date) -> list[str]:
    """Ways an annual report writes its year-end date."""
    month = _MONTHS[d.month - 1]
    return [f"{d.day} {month} {d.year}", f"{d.day} {month[:3]} {d.year}",
            f"{d.day:02d} {month} {d.year}", f"{month} {d.day}, {d.year}",
            f"{d.day:02d}/{d.month:02d}/{d.year}", d.isoformat()]


# --- page selection --------------------------------------------------------------------

# topic -> (heading patterns, needs figures on the page)
TOPICS: dict[str, tuple[re.Pattern[str], bool]] = {
    "income_statement": (re.compile(
        r"(consolidated |group )?(income statement|profit and loss account|statement of "
        r"(comprehensive income|profit or loss))", re.I), True),
    "balance_sheet": (re.compile(
        r"(consolidated |group )?(balance sheet|statement of financial position)", re.I), True),
    "cash_flow": (re.compile(r"(cash flow statement|statement of cash flows)", re.I), True),
    "highlights": (re.compile(r"(financial highlights|key performance indicators|at a glance|"
                              r"financial review)", re.I), True),
    "employees": (re.compile(r"average (monthly )?number of (persons |people )?employ", re.I), False),
    "activities": (re.compile(r"principal activit", re.I), False),
    "shareholders": (re.compile(r"(substantial|significant|major) (share)?holdings?|"
                                r"interests in (voting rights|shares)|major shareholders", re.I), False),
    "parent": (re.compile(r"ultimate (parent|controlling party|holding company)", re.I), False),
    "auditor": (re.compile(r"independent auditors?['’]? report", re.I), False),
}
FIGURE_TOPICS = ("income_statement", "balance_sheet", "cash_flow", "highlights")
# Topics often mentioned in passing ("engagement with major shareholders"): need a heading.
TITLE_TOPICS = ("shareholders", "parent")
_NUMBER = re.compile(r"\(?\d{1,3}(?:[,\s]\d{3})+(?:\.\d+)?\)?|\(?\d+\.\d+\)?|\(?\d{3,}\)?")


def _figures(text: str) -> int:
    return len(_NUMBER.findall(text))


def _title_line(text: str, pattern: re.Pattern[str]) -> str | None:
    """The line where the pattern appears as a heading: it starts the line (after an
    optional note number), the line is short, and it doesn't read as part of a sentence
    ("...recognised in the income statement.", "Statement of income below:")."""
    for raw in text.splitlines():
        line = re.sub(r"^\s*(\d{1,3}[.)]?\s+)?", "", raw).strip()
        m = pattern.search(line)
        if not m or m.start() > 3 or len(line) > len(m.group(0)) + 25:
            continue
        if line.endswith((".", ":", ",", ";")) or re.search(r"\b(the|our|in|per|of)\s*$", line[:m.start()], re.I):
            continue
        return line
    return None


def _title_hit(text: str, pattern: re.Pattern[str]) -> bool:
    return _title_line(text, pattern) is not None


def _looks_like_contents(text: str) -> bool:
    """A contents page names every statement next to a page number."""
    hits = sum(1 for pattern, _ in TOPICS.values() if pattern.search(text))
    return hits >= 4 and _figures(text) < 60


def select_pages(texts: list[str], *, figure_pages: int = 6, other_pages: int = 6) -> list[PdfPage]:
    """Pick the pages worth reading: the best page for each financial statement (a
    heading plus plenty of figures), and the pages that mention employees, activities,
    shareholders, the parent and the auditor."""
    chosen: dict[int, PdfPage] = {}
    for topic, (pattern, needs_figures) in TOPICS.items():
        best: tuple[float, int] | None = None
        for i, text in enumerate(texts):
            if not text or not pattern.search(text) or _looks_like_contents(text):
                continue
            if (needs_figures or topic in TITLE_TOPICS) and not _title_hit(text, pattern):
                continue
            score = float(_figures(text)) if needs_figures else 1.0 / (1 + i) + len(pattern.findall(text))
            if needs_figures and score < 15:
                continue  # a heading with no table under it
            title = _title_line(text, pattern) or ""
            if needs_figures and re.search(r"consolidated|group", title, re.I):
                score *= 2  # the group's primary statement, not a note or a company-only one
            if best is None or score > best[0]:
                best = (score, i)
        if best is not None:
            page = chosen.setdefault(best[1], PdfPage(number=best[1] + 1, text=texts[best[1]]))
            page.topics.append(topic)

    figures = [p for p in chosen.values() if set(p.topics) & set(FIGURE_TOPICS)]
    others = [p for p in chosen.values() if not set(p.topics) & set(FIGURE_TOPICS)]
    keep = sorted(figures, key=lambda p: p.number)[:figure_pages] + \
        sorted(others, key=lambda p: p.number)[:other_pages]
    return sorted(keep, key=lambda p: p.number)


# --- text PDFs ------------------------------------------------------------------------------


def pdf_page_texts(data: bytes) -> list[str]:
    from pypdf import PdfReader

    logging.getLogger("pypdf").setLevel(logging.ERROR)  # font and xref warnings are noise here
    try:
        reader = PdfReader(io.BytesIO(data))
        return [(page.extract_text() or "") for page in reader.pages]
    except Exception:  # damaged or encrypted PDFs: treat as unreadable
        return []


def has_text_layer(texts: list[str]) -> bool:
    return sum(len(t.strip()) for t in texts) > 200 * max(1, min(len(texts), 20)) // 2


# --- finding the company's own annual report --------------------------------------------------


def _brand(host: str) -> str:
    return site_host(host).split(".")[0]


def _same_brand(url: str, website: str | None, brand_words: set[str]) -> bool:
    """Investor sites are often on another domain (corporate.greggs.co.uk for greggs.com)."""
    host = urlparse(url).netloc.lower()
    if website and site_host(url).endswith(site_host(website).split(".", 1)[-1]) and \
            _brand(website) in host:
        return True
    return any(w in host for w in brand_words if len(w) >= 4)


def report_candidates(links: list[tuple[str, str]], year: int) -> list[tuple[int, str]]:
    """Score (url, link text) pairs that look like this year's annual report PDF."""
    yy = f"{year % 100:02d}"
    scored = []
    for url, text in links:
        if not _PDF_LINK.search(url):
            continue
        hay = f"{url} {text}"
        if _NOT_REPORT.search(hay) or not _REPORT_WORDS.search(hay):
            continue
        score = 10
        if str(year) in hay:
            score += 20
        elif re.search(rf"(^|[^0-9]){yy}([^0-9]|$)", url):
            score += 12
        elif re.search(r"(19|20)\d\d", hay):
            score -= 15  # names a different year
        scored.append((score, url))
    scored.sort(key=lambda s: -s[0])
    return scored


def verify_report(texts: list[str], company_name: str, made_up: date) -> bool:
    """The report must name the company and its exact year-end date."""
    head = "\n".join(texts[:40] + texts[-60:]).lower()
    core = core_name(company_name)
    return bool(core) and core.split()[0] in head and \
        any(form.lower() in head for form in date_forms(made_up))


def find_annual_report(crawler, *, company_name: str, made_up: date, website: str | None,
                       seed_links: list[tuple[str, str]], http: httpx.Client) -> tuple[str, list[str]] | None:
    """Look for the text PDF of the annual report for the year ending `made_up`.
    Returns (url, page texts) or None."""
    brand_words = set(core_name(company_name).split()) | ({_brand(website)} if website else set())
    links = list(seed_links)

    # Follow annual-report, investor and corporate pages (also on the brand's other
    # domains, e.g. corporate.greggs.co.uk), up to two levels deep.
    def investor_pages(candidates: list[tuple[str, str]]) -> list[str]:
        return [u for u, t in candidates if u.startswith("http") and not _PDF_LINK.search(u)
                and _INVESTOR_PAGE.search(f"{urlparse(u).netloc} {urlparse(u).path} {t}")
                and _same_brand(u, website, brand_words)]

    visited: set[str] = set()
    frontier = investor_pages(links)
    for _ in range(2):
        found: list[tuple[str, str]] = []
        for url in list(dict.fromkeys(frontier)):
            if len(visited) >= MAX_HOPS or url in visited:
                continue
            visited.add(url)
            page = crawler.fetch(url, "investors", [])
            if page is not None and page.status_code == 200:
                found.extend(page.links)
        links.extend(found)
        if report_candidates(links, made_up.year):
            break
        frontier = investor_pages(found)

    for _, url in report_candidates(links, made_up.year)[:MAX_CANDIDATES]:
        if not crawler.allowed(url):
            continue
        try:
            resp = http.get(url)
        except httpx.HTTPError:
            continue
        if resp.status_code != 200 or len(resp.content) > MAX_PDF_BYTES or \
                not resp.content.startswith(b"%PDF"):
            continue
        texts = pdf_page_texts(resp.content)
        if has_text_layer(texts) and verify_report(texts, company_name, made_up):
            return url, texts
    return None


# --- OCR of the Companies House scan --------------------------------------------------------


def tesseract_path() -> str | None:
    found = os.environ.get("TESSERACT_CMD") or shutil.which("tesseract")
    if found:
        return found
    default = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    return default if os.path.exists(default) else None


def _page_images(data: bytes) -> list:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    images = []
    for page in reader.pages:
        try:
            imgs = page.images
            images.append(imgs[0].image if imgs else None)
        except Exception:
            images.append(None)
    return images


def _ocr(image, width: int) -> str:
    import pytesseract

    if image is None:
        return ""
    img = image.convert("L")
    if img.width > width:
        img = img.resize((width, max(1, int(img.height * width / img.width))))
    try:
        return pytesseract.image_to_string(img)
    except Exception:
        return ""


def ocr_document(data: bytes, *, cache=None, cache_key: str = "", workers: int | None = None,
                 progress=None) -> tuple[list[str], list[PdfPage]]:
    """OCR every page quickly, choose the pages worth reading, then OCR those at a
    higher resolution. Returns (quick texts, chosen pages)."""
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = tesseract_path()
    images = _page_images(data)
    workers = workers or max(2, (os.cpu_count() or 4) - 1)

    quick = (cache.get(cache_key + "#quick") or {}).get("pages") if cache is not None else None
    if quick is None or len(quick) != len(images):
        if progress:
            progress(f"Reading {len(images)} scanned pages (quick pass)")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            quick = list(pool.map(lambda im: _ocr(im, QUICK_OCR_WIDTH), images))
        if cache is not None:
            cache.put(cache_key + "#quick", {"pages": quick})

    chosen = select_pages(quick)
    sharp = (cache.get(cache_key + "#sharp") or {}).get("pages", {}) if cache is not None else {}
    todo = [p for p in chosen if str(p.number) not in sharp]
    if todo:
        if progress:
            progress(f"Reading {len(todo)} chosen pages at full detail")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            texts = list(pool.map(lambda p: _ocr(images[p.number - 1], FULL_OCR_WIDTH), todo))
        sharp.update({str(p.number): t for p, t in zip(todo, texts)})
        if cache is not None:
            cache.put(cache_key + "#sharp", {"pages": sharp})
    for p in chosen:
        p.text = sharp.get(str(p.number)) or p.text
    return quick, chosen


# --- putting it together ----------------------------------------------------------------------


def get_accounts_document(client: CompaniesHouseClient, number: str, company_name: str, *,
                          crawler=None, website: str | None = None,
                          seed_links: list[tuple[str, str]] | None = None, cache=None,
                          progress=None) -> AccountsDocument | None:
    """The pages of the latest accounts worth reading, or None if the latest accounts
    are already machine-readable (iXBRL) or nothing could be read."""
    item = latest_accounts_filing(client, number)
    if item is None:
        return None
    meta = item["links"]["document_metadata"]
    if IXBRL in client.document_formats(meta):
        return None  # accounts.py reads those exactly
    made_up = made_up_date(item)
    notes: list[str] = []

    if crawler is not None and made_up is not None:
        if progress:
            progress("Looking for the company's own annual report")
        with httpx.Client(timeout=60, follow_redirects=True,
                          headers={"User-Agent": getattr(crawler, "user_agent", None)
                                   or "Mozilla/5.0 (compatible; informant/0.1; personal company research)"}) as http:
            found = find_annual_report(crawler, company_name=company_name, made_up=made_up,
                                       website=website, seed_links=seed_links or [], http=http)
        if found is not None:
            url, texts = found
            return AccountsDocument("annual_report", url, made_up, item.get("date"), len(texts),
                                    select_pages(texts), notes)
        notes.append("the company's own annual report PDF wasn't found")

    if tesseract_path() is None:
        notes.append("the filed accounts are a scanned image and Tesseract (OCR) isn't installed")
        return AccountsDocument("ocr", filing_document_url(number, item) or meta, made_up,
                                item.get("date"), 0, [], notes)
    try:
        data = client.document_content(meta, PDF)
    except CompaniesHouseError as e:
        notes.append(f"the filed accounts PDF couldn't be downloaded: {e}")
        return AccountsDocument("ocr", meta, made_up, item.get("date"), 0, [], notes)
    texts = pdf_page_texts(data)
    if has_text_layer(texts):  # a filed PDF that does have text after all
        return AccountsDocument("annual_report", filing_document_url(number, item) or meta, made_up,
                                item.get("date"), len(texts), select_pages(texts), notes)
    quick, chosen = ocr_document(data, cache=cache, cache_key=meta, progress=progress)
    return AccountsDocument("ocr", filing_document_url(number, item) or meta, made_up,
                            item.get("date"), len(quick), chosen, notes)
