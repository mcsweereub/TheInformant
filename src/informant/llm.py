"""Turn crawled page text into structured fields with an LLM.

Two backends share one prompt and one output schema:
  * claude: Anthropic API (paid). Needs ANTHROPIC_API_KEY.
            INFORMANT_MODEL / INFORMANT_EFFORT override model and effort.
  * ollama: local model (free). Needs Ollama running (https://ollama.com).
            OLLAMA_MODEL, OLLAMA_HOST and OLLAMA_NUM_CTX override the defaults.

INFORMANT_LLM picks the backend: "claude", "ollama", or "auto" (the default:
Claude if ANTHROPIC_API_KEY is set, otherwise Ollama).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

import anthropic
import httpx
from pydantic import BaseModel, ValidationError

from .crawler import Page

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "low"  # extraction from supplied text is a simple task
DEFAULT_OLLAMA_MODEL = "qwen3:4b"  # gemma4:12b timed out (>15 min) on a CPU-only laptop
DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_OLLAMA_NUM_CTX = 16384  # tokens; Ollama's own default is too small for web pages
PAGE_CHAR_LIMIT = 12_000
TOTAL_CHAR_LIMIT = 60_000
OLLAMA_TIMEOUT_S = 1200.0  # 20 min: local models on a CPU are slow with long input
OLLAMA_TOTAL_CHAR_LIMIT = 28_000  # ~7k tokens, leaves room in the context for prompt + output

# A fixed vocabulary keeps categorisation consistent across companies.
SECTORS = [
    "Agriculture & Food Production", "Automotive", "Construction & Real Estate",
    "Consumer Goods", "Creative & Media", "Defence & Aerospace", "Education",
    "Energy & Utilities", "Engineering & Manufacturing", "Financial Services",
    "Food & Drink Retail", "Government & Public Sector", "Healthcare & Life Sciences",
    "Hospitality & Leisure", "Insurance", "Legal & Professional Services",
    "Logistics & Transport", "Marketing & Advertising", "Mining & Materials",
    "Non-profit & Charity", "Retail & E-commerce", "Software & IT Services",
    "Telecommunications", "Travel & Tourism", "Other",
]

SYSTEM_PROMPT = f"""You read the text of a company's own website and record facts about that company for a personal research database. You reply with JSON only.

Work only from the website text you are given. When the pages don't state something, use an empty list, or the word unknown for a single value. Don't add facts from general knowledge.

Record facts about the company, never about people: leave out the names and details of individual employees, directors and private customers.

What to record:
Write a summary of two or three plain sentences saying what the company does and who its customers are.
Choose between one and three sectors, using only these exact names: {"; ".join(SECTORS)}.
Give between three and ten tags: short lowercase keywords that would help group similar companies, such as bakery, franchise, saas or b2b payments.
Give the business model as exactly one of: B2B, B2C, B2B2C, marketplace, public sector, mixed, unknown.
List the target markets: the kinds of customers or the regions the company sells to.
List up to 15 of the main products, services or product lines, each with a one-sentence description and a short category.
List up to 15 physical sites that the pages name, such as offices, factories or flagship locations. For a chain with hundreds of shops, give only the headquarters and mention the shop network in the summary.
For each product and each site, set source_page to the web address shown on the PAGE line of the page where you found it.

Every value you write must describe this particular company and come from its pages. Never copy wording from these instructions into your answer."""


class ExtractedProduct(BaseModel):
    name: str
    description: str
    category: str
    source_page: str  # URL of the page it came from (checked against the crawled pages)


class ExtractedOffice(BaseModel):
    label: str  # e.g. "Head office", "Manchester office"
    address: str
    locality: str
    country: str
    source_page: str


class WebsiteExtraction(BaseModel):
    summary: str
    business_model: str
    sectors: list[str]
    tags: list[str]
    target_markets: list[str]
    products: list[ExtractedProduct]
    offices: list[ExtractedOffice]


@dataclass
class ExtractionResult:
    data: WebsiteExtraction | None
    model: str | None = None
    notes: list[str] = field(default_factory=list)


def remove_repeated_lines(pages: list[Page]) -> list[str]:
    """Each page's text with any line already seen (on this or an earlier page)
    removed. Menus, cookie banners and footers repeat on every page; the model
    only needs them once. Pages are in crawl order, so the homepage keeps its copy."""
    seen: set[str] = set()
    texts = []
    for page in pages:
        kept = []
        for line in page.text.splitlines():
            key = " ".join(line.lower().split())
            if not key or key in seen:
                continue
            seen.add(key)
            kept.append(line)
        texts.append("\n".join(kept))
    return texts


def fair_shares(lengths: list[int], budget: int, cap: int = PAGE_CHAR_LIMIT) -> list[int]:
    """Split `budget` characters between pages so each gets an equal share, up to
    `cap`; pages needing less than their share pass the spare on to the others.
    This way a long page (e.g. legal text) can't crowd out later ones."""
    shares = [0] * len(lengths)
    remaining = budget
    pending = [i for i, n in enumerate(lengths) if n > 0]
    # Smallest pages first: they take what they need, the rest split what's left.
    for pos, i in enumerate(sorted(pending, key=lambda i: lengths[i])):
        fair = remaining // (len(pending) - pos)
        shares[i] = min(lengths[i], cap, fair)
        remaining -= shares[i]
    return shares


def build_page_text(pages: list[Page], total_limit: int = TOTAL_CHAR_LIMIT) -> tuple[str, list[str]]:
    notes: list[str] = []
    texts = remove_repeated_lines(pages)
    original = sum(len(p.text) for p in pages)
    deduped = sum(len(t) for t in texts)
    if original - deduped >= 500:
        notes.append(f"removed {original - deduped:,} characters of text repeated across pages "
                     f"(menus, footers); {deduped:,} left")

    shares = fair_shares([len(t) if t.strip() else 0 for t in texts], total_limit)
    parts: list[str] = []
    for page, text, share in zip(pages, texts, shares):
        if share == 0:
            continue  # nothing on this page that wasn't already sent
        if len(text) > share:
            notes.append(f"{page.url}: sent first {share:,} of {len(text):,} characters to the LLM")
            text = text[:share]
        parts.append(f"=== PAGE ({page.kind}): {page.url}\nTitle: {page.title or ''}\n\n{text}")
    return "\n\n".join(parts), notes


def _user_message(company_name: str, page_text: str) -> str:
    # The closing instruction comes after the pages: small local models follow
    # what they read last most closely.
    return (
        f"Company: {company_name}\n\nWebsite pages:\n\n{page_text}\n\n"
        f"=== END OF PAGES\n\nNow write the JSON record for {company_name}, "
        "using only facts from the pages above."
    )


def _copied_instructions(data: WebsiteExtraction) -> bool:
    """True if the model echoed prompt wording instead of extracting facts
    (small local models sometimes fill fields with the instructions)."""
    prompt = SYSTEM_PROMPT.lower()
    placeholders = {"product name", "short category", "one-sentence description",
                    "office name", "city or town", "country"}

    def echoed(value: str) -> bool:
        v = value.strip().lower().rstrip(".")
        return v in placeholders or (len(v) > 25 and v in prompt)

    if echoed(data.summary) or echoed(data.business_model):
        return True
    values = [p.name for p in data.products] + [p.description for p in data.products]
    values += data.target_markets + [o.label for o in data.offices]
    return bool(values) and sum(echoed(v) for v in values) >= max(1, len(values) // 3)


def _finish(data: WebsiteExtraction, model: str | None, notes: list[str]) -> ExtractionResult:
    if _copied_instructions(data):
        return ExtractionResult(None, model=model, notes=notes + [
            f"{model} copied the prompt instead of extracting facts; result discarded "
            "(try a larger model)"])
    return ExtractionResult(_clean(data), model=model, notes=notes)


def _clean(data: WebsiteExtraction) -> WebsiteExtraction:
    data.sectors = list(dict.fromkeys(s for s in data.sectors if s in SECTORS)) or ["Other"]
    data.tags = sorted({t.strip().lower() for t in data.tags if t.strip()})
    return data


# Claude models offered by `informant model`, with rough cost per company
# (~20k input + ~1.5k output tokens).
CLAUDE_MODELS: list[tuple[str, str]] = [
    ("claude-opus-5-5", "most capable, about 9p per company"),
    ("claude-sonnet-5-5", "balanced, about 4p per company"),
    ("claude-haiku-5-5", "cheapest Claude, about 0.3p per company"),
]


@dataclass
class ModelOption:
    backend: str  # claude / ollama
    model: str
    notes: str


def ollama_host() -> str:
    host = os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST).rstrip("/")
    return host if host.startswith("http") else "http://" + host


def list_ollama_models(http: httpx.Client | None = None) -> list[ModelOption] | None:
    """Installed local models, or None if Ollama isn't running."""
    try:
        if http is None:
            resp = httpx.get(f"{ollama_host()}/api/tags", timeout=5)
        else:
            resp = http.get(f"{ollama_host()}/api/tags")
        resp.raise_for_status()
    except (httpx.HTTPError, ValueError):
        return None
    options = []
    for m in resp.json().get("models", []):
        d = m.get("details") or {}
        size_gb = (m.get("size") or 0) / 1e9
        bits = [x for x in (d.get("parameter_size"), d.get("quantization_level")) if x]
        bits.append(f"{size_gb:.1f} GB" if size_gb else "")
        options.append(ModelOption("ollama", m["name"], "free, local: " + ", ".join(b for b in bits if b)))
    return options


def model_options() -> tuple[list[ModelOption], bool]:
    """All selectable models, and whether Ollama was reachable."""
    local = list_ollama_models()
    options = [ModelOption("claude", m, n) for m, n in CLAUDE_MODELS] + (local or [])
    return options, local is not None


def current_model() -> tuple[str, str]:
    backend = choose_backend()
    if backend == "claude":
        return backend, os.environ.get("INFORMANT_MODEL", DEFAULT_MODEL)
    return backend, os.environ.get("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)


def choose_backend() -> str:
    choice = os.environ.get("INFORMANT_LLM", "auto").strip().lower()
    if choice in ("claude", "ollama"):
        return choice
    return "claude" if os.environ.get("ANTHROPIC_API_KEY") else "ollama"


def extract_with_llm(company_name: str, pages: list[Page],
                     client: anthropic.Anthropic | None = None) -> ExtractionResult:
    """Run whichever backend is configured. Passing an Anthropic client forces Claude."""
    if client is not None or choose_backend() == "claude":
        return extract_with_claude(company_name, pages, client)
    return extract_with_ollama(company_name, pages)


# --- Claude ---------------------------------------------------------------


def extract_with_claude(company_name: str, pages: list[Page],
                        client: anthropic.Anthropic | None = None) -> ExtractionResult:
    model = os.environ.get("INFORMANT_MODEL", DEFAULT_MODEL)
    effort = os.environ.get("INFORMANT_EFFORT", DEFAULT_EFFORT)
    page_text, notes = build_page_text(pages)
    if not page_text:
        return ExtractionResult(None, notes=["no page text to send to the LLM"])

    try:
        client = client or anthropic.Anthropic()
        response = client.beta.messages.parse(
            model=model,
            max_tokens=16000,
            # If a safety classifier declines, the API retries on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": effort},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _user_message(company_name, page_text)}],
            output_format=WebsiteExtraction,
        )
    except anthropic.AuthenticationError:
        return ExtractionResult(None, notes=notes + ["Anthropic API key rejected; LLM extraction skipped"])
    except anthropic.APIStatusError as e:
        return ExtractionResult(None, notes=notes + [f"LLM request failed ({e.status_code}): {e.message}"])
    except anthropic.APIConnectionError:
        return ExtractionResult(None, notes=notes + ["could not reach the Anthropic API; LLM extraction skipped"])
    except anthropic.AnthropicError as e:  # e.g. no credentials configured
        return ExtractionResult(None, notes=notes + [f"LLM extraction skipped: {e}"])

    if response.stop_reason == "refusal":
        return ExtractionResult(None, model=response.model, notes=notes + ["LLM declined this request"])
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        return ExtractionResult(None, model=response.model, notes=notes + ["LLM output was incomplete; skipped"])

    return _finish(response.parsed_output, response.model, notes)


# --- Ollama (local, free) -------------------------------------------------


def extract_with_ollama(company_name: str, pages: list[Page],
                        http: httpx.Client | None = None) -> ExtractionResult:
    model = os.environ.get("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
    host = ollama_host()
    num_ctx = int(os.environ.get("OLLAMA_NUM_CTX", DEFAULT_OLLAMA_NUM_CTX))
    label = f"ollama/{model}"

    page_text, notes = build_page_text(pages, total_limit=OLLAMA_TOTAL_CHAR_LIMIT)
    if not page_text:
        return ExtractionResult(None, notes=["no page text to send to the LLM"])

    own_client = http is None
    # Local models on a CPU can take several minutes, so allow a long read timeout.
    http = http or httpx.Client(timeout=httpx.Timeout(OLLAMA_TIMEOUT_S, connect=5.0))
    request = {
        "model": model,
        "stream": False,
        "format": WebsiteExtraction.model_json_schema(),  # constrains output to the schema
        "options": {"temperature": 0, "num_ctx": num_ctx},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _user_message(company_name, page_text)},
        ],
    }
    started = time.monotonic()
    try:
        # Thinking models (e.g. qwen3) reason at length before answering, which is
        # slow on a CPU and not needed for extraction. Only models that support
        # thinking accept the flag, so ask Ollama first.
        if _ollama_supports_thinking(http, host, model):
            request["think"] = False
        resp = http.post(f"{host}/api/chat", json=request)
    except httpx.ConnectError:
        return ExtractionResult(None, notes=notes + [
            f"could not reach Ollama at {host}; start the Ollama app, or use --no-llm"])
    except httpx.TimeoutException:
        return ExtractionResult(None, notes=notes + [
            f"{label} didn't finish within {OLLAMA_TIMEOUT_S / 60:.0f} minutes; "
            "try a smaller model, or Claude"])
    finally:
        if own_client:
            http.close()

    if resp.status_code == 404:
        return ExtractionResult(None, notes=notes + [
            f"Ollama model '{model}' isn't installed; run: ollama pull {model}"])
    if resp.is_error:
        return ExtractionResult(None, notes=notes + [
            f"Ollama request failed ({resp.status_code}): {resp.text[:200]}"])

    body = resp.json()
    if body.get("done_reason") == "length":
        return ExtractionResult(None, model=label, notes=notes + [
            f"{label} ran out of context; raise OLLAMA_NUM_CTX"])
    try:
        data = WebsiteExtraction.model_validate_json(body.get("message", {}).get("content", ""))
    except ValidationError:
        return ExtractionResult(None, model=label, notes=notes + [
            f"{label} returned output that didn't match the schema; skipped"])
    elapsed = time.monotonic() - started
    notes.append(f"{label} took {elapsed:.0f}s")
    return _finish(data, label, notes)


def _ollama_supports_thinking(http: httpx.Client, host: str, model: str) -> bool:
    try:
        resp = http.post(f"{host}/api/show", json={"model": model}, timeout=10)
        return resp.status_code == 200 and "thinking" in (resp.json().get("capabilities") or [])
    except (httpx.HTTPError, ValueError):
        return False
