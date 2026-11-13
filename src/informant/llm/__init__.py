"""Turning raw page text into structured fields with an LLM.

    models.py     which backend/model to use; the `informant model` list
    prompts.py    the prompt, fixed SECTORS list and output schema (shared by all backends)
    budget.py     drop repeated lines, fair share of the input per page
    result.py     ExtractionResult, plus checks applied to every answer
    backends/     claude.py (paid API) and ollama.py (free, local)
    staged.py     staged extraction: notes per part, then one merge call
    extract.py    extract_with_llm(): pick backend + strategy (single / staged / auto)

The names below are the package's public API.
"""

from .backends.claude import extract_with_claude
from .backends.ollama import extract_with_ollama
from .budget import build_page_text, fair_shares, remove_repeated_lines
from .extract import STRATEGIES, choose_strategy, extract_with_llm, make_complete
from .news import judge_headlines
from .models import ModelOption, choose_backend, current_model, model_options
from .prompts import SECTORS, WebsiteExtraction
from .result import ExtractionResult
from .staged import extract_staged, make_chunks

__all__ = [
    "SECTORS", "STRATEGIES", "ExtractionResult", "ModelOption", "WebsiteExtraction",
    "build_page_text", "choose_backend", "choose_strategy", "current_model",
    "extract_staged", "extract_with_claude", "extract_with_llm", "extract_with_ollama",
    "judge_headlines", "make_complete",
    "fair_shares", "make_chunks", "model_options", "remove_repeated_lines",
]
