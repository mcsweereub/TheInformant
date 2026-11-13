"""Single-call extraction: all pages (up to a character budget) in one request."""

from __future__ import annotations

from .backends.base import CompleteFn
from .budget import build_page_text
from .prompts import SYSTEM_PROMPT, WebsiteExtraction, user_message
from .result import ExtractionResult, finish


def extract_single(company_name: str, pages: list, complete: CompleteFn,
                   total_limit: int) -> ExtractionResult:
    page_text, notes = build_page_text(pages, total_limit=total_limit)
    if not page_text:
        return ExtractionResult(None, notes=["no page text to send to the LLM"], strategy="single")
    c = complete(SYSTEM_PROMPT, user_message(company_name, page_text), WebsiteExtraction)
    if c.data is None:
        return ExtractionResult(None, model=c.model, notes=notes + c.notes, strategy="single")
    if c.cached:
        notes.append("LLM answer reused from the local cache (same model, prompt and page text)")
    elif c.model and c.model.startswith("ollama/"):
        notes.append(f"{c.model} took {c.elapsed:.0f}s")
    result = finish(c.data, c.model, notes, source_text=page_text,
                    company_name=company_name)
    result.strategy = "single"
    return result
