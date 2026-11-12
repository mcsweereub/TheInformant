"""Which LLM backend and model to use, and the list offered by `informant model`.

INFORMANT_LLM picks the backend: "claude", "ollama", or "auto" (the default:
Claude if ANTHROPIC_API_KEY is set, otherwise Ollama). INFORMANT_MODEL and
OLLAMA_MODEL pick the model for each backend.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_OLLAMA_MODEL = "qwen3:4b"  # gemma4:12b timed out (>15 min) on a CPU-only laptop
DEFAULT_OLLAMA_HOST = "http://localhost:11434"

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


def choose_backend() -> str:
    choice = os.environ.get("INFORMANT_LLM", "auto").strip().lower()
    if choice in ("claude", "ollama"):
        return choice
    return "claude" if os.environ.get("ANTHROPIC_API_KEY") else "ollama"


def current_model() -> tuple[str, str]:
    backend = choose_backend()
    if backend == "claude":
        return backend, os.environ.get("INFORMANT_MODEL", DEFAULT_MODEL)
    return backend, os.environ.get("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)


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
