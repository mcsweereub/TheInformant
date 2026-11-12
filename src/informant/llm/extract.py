"""Run the configured backend over a company's material, in one call or in stages.

Strategy (INFORMANT_LLM_STRATEGY or --llm-strategy):
  single  everything in one call
  staged  notes per part, then a merge call (see staged.py)
  auto    Claude: single (its context is huge, and fewer calls cost less).
          Ollama: single when the text fits comfortably, otherwise staged.
"""

from __future__ import annotations

import os

import anthropic

from ..errors import ConfigError
from ..progress import ProgressFn
from .backends import claude, ollama
from .models import choose_backend
from .result import ExtractionResult
from .staged import extract_staged, needs_staging

STRATEGIES = ("auto", "single", "staged")
OLLAMA_SINGLE_LIMIT = 12_000  # above this (after removing repeats), auto goes staged


def choose_strategy(backend: str, pages: list, requested: str | None = None) -> str:
    strategy = (requested or os.environ.get("INFORMANT_LLM_STRATEGY") or "auto").strip().lower()
    if strategy not in STRATEGIES:
        raise ConfigError(f"LLM strategy must be auto, single or staged, not {strategy!r}.")
    if strategy != "auto":
        return strategy
    if backend == "claude":
        return "single"
    return "staged" if needs_staging(pages, OLLAMA_SINGLE_LIMIT) else "single"


def extract_with_llm(company_name: str, pages: list,
                     client: anthropic.Anthropic | None = None, *,
                     strategy: str | None = None,
                     progress: ProgressFn | None = None) -> ExtractionResult:
    """Run whichever backend is configured. Passing an Anthropic client forces Claude."""
    backend = "claude" if client is not None or choose_backend() == "claude" else "ollama"
    chosen = choose_strategy(backend, pages, strategy)

    if chosen == "single":
        if backend == "claude":
            result = claude.extract_with_claude(company_name, pages, client)
        else:
            result = ollama.extract_with_ollama(company_name, pages)
        result.strategy = "single"
        return result

    if backend == "claude":
        def complete(system, user, schema):
            return claude.complete(system, user, schema, client)
    else:
        complete = ollama.complete
    return extract_staged(company_name, pages, complete, progress)
