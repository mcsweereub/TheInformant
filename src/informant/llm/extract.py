"""Run the configured backend over a company's material, in one call or in stages.

Strategy (INFORMANT_LLM_STRATEGY or --llm-strategy):
  single  everything in one call
  staged  notes per part, then a merge call (see staged.py)
  auto    Claude: single (its context is huge, and fewer calls cost less).
          Ollama: single when the text fits comfortably, otherwise staged.

Pass an LlmCache to answer repeated, identical calls from disk.
"""

from __future__ import annotations

import os

import anthropic

from ..errors import ConfigError
from ..progress import ProgressFn
from .backends import claude, ollama
from .backends.base import CompleteFn, with_cache
from .budget import OLLAMA_TOTAL_CHAR_LIMIT, TOTAL_CHAR_LIMIT
from .models import DEFAULT_MODEL, choose_backend
from .result import ExtractionResult
from .single import extract_single
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


def _model_id(backend: str) -> str:
    """Identifies the configured model for cache keys (before any call is made)."""
    if backend == "claude":
        model = os.environ.get("INFORMANT_MODEL", DEFAULT_MODEL)
        effort = os.environ.get("INFORMANT_EFFORT", claude.DEFAULT_EFFORT)
        return f"claude/{model}/effort={effort}"
    return ollama.model_label()


def extract_with_llm(company_name: str, pages: list,
                     client: anthropic.Anthropic | None = None, *,
                     strategy: str | None = None,
                     progress: ProgressFn | None = None,
                     cache=None) -> ExtractionResult:
    """Run whichever backend is configured. Passing an Anthropic client forces Claude."""
    backend = "claude" if client is not None or choose_backend() == "claude" else "ollama"
    chosen = choose_strategy(backend, pages, strategy)

    complete: CompleteFn
    if backend == "claude":
        def complete(system, user, schema):
            return claude.complete(system, user, schema, client)
    else:
        complete = ollama.complete
    if cache is not None:
        complete = with_cache(complete, cache, _model_id(backend))

    if chosen == "single":
        limit = TOTAL_CHAR_LIMIT if backend == "claude" else OLLAMA_TOTAL_CHAR_LIMIT
        return extract_single(company_name, pages, complete, limit)
    return extract_staged(company_name, pages, complete, progress)
