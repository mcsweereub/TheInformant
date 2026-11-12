"""Run the configured backend over a company's crawled pages."""

from __future__ import annotations

import anthropic

from .backends.claude import extract_with_claude
from .backends.ollama import extract_with_ollama
from .models import choose_backend
from .result import ExtractionResult


def extract_with_llm(company_name: str, pages: list,
                     client: anthropic.Anthropic | None = None) -> ExtractionResult:
    """Run whichever backend is configured. Passing an Anthropic client forces Claude."""
    if client is not None or choose_backend() == "claude":
        return extract_with_claude(company_name, pages, client)
    return extract_with_ollama(company_name, pages)
