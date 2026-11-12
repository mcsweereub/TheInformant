"""Turning raw page text into structured fields with an LLM.

    models.py     which backend/model to use; the `informant model` list
    prompts.py    the prompt, fixed SECTORS list and output schema (shared by all backends)
    budget.py     drop repeated lines, fair share of the input per page
    result.py     ExtractionResult, plus checks applied to every answer
    backends/     claude.py (paid API) and ollama.py (free, local)
    extract.py    extract_with_llm(): run whichever backend is configured

The names below are the package's public API.
"""

from .backends.claude import extract_with_claude
from .backends.ollama import extract_with_ollama
from .budget import build_page_text, fair_shares, remove_repeated_lines
from .extract import extract_with_llm
from .models import ModelOption, choose_backend, current_model, model_options
from .prompts import SECTORS, WebsiteExtraction
from .result import ExtractionResult

__all__ = [
    "SECTORS", "ExtractionResult", "ModelOption", "WebsiteExtraction",
    "build_page_text", "choose_backend", "current_model", "extract_with_claude",
    "extract_with_llm", "extract_with_ollama", "fair_shares", "model_options",
    "remove_repeated_lines",
]
