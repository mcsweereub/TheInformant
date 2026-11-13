"""Ollama (local model, free). Needs Ollama running (https://ollama.com).
OLLAMA_MODEL, OLLAMA_HOST and OLLAMA_NUM_CTX override the defaults."""

from __future__ import annotations

import os
import time

import httpx
from pydantic import BaseModel, ValidationError

from ..budget import OLLAMA_TOTAL_CHAR_LIMIT
from ..models import DEFAULT_OLLAMA_MODEL, ollama_host
from ..result import ExtractionResult
from ..single import extract_single
from .base import Completion

DEFAULT_OLLAMA_NUM_CTX = 16384  # tokens; Ollama's own default is too small for web pages
# Most answers need well under 2,000 tokens. The cap stops a model that starts repeating
# itself from running for many minutes (OLLAMA_NUM_PREDICT overrides it).
DEFAULT_OLLAMA_NUM_PREDICT = 4096
OLLAMA_TIMEOUT_S = 1200.0  # 20 min per call: local models on a CPU are slow with long input


def model_label() -> str:
    return f"ollama/{os.environ.get('OLLAMA_MODEL', DEFAULT_OLLAMA_MODEL)}"


def complete(system: str, user: str, schema: type[BaseModel],
             http: httpx.Client | None = None) -> Completion:
    """One structured call: returns an instance of `schema`, or None with a reason."""
    model = os.environ.get("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
    host = ollama_host()
    num_ctx = int(os.environ.get("OLLAMA_NUM_CTX", DEFAULT_OLLAMA_NUM_CTX))
    num_predict = int(os.environ.get("OLLAMA_NUM_PREDICT", DEFAULT_OLLAMA_NUM_PREDICT))
    label = f"ollama/{model}"

    own_client = http is None
    # Local models on a CPU can take several minutes, so allow a long read timeout.
    http = http or httpx.Client(timeout=httpx.Timeout(OLLAMA_TIMEOUT_S, connect=5.0))
    request = {
        "model": model,
        "stream": False,
        "format": schema.model_json_schema(),  # constrains output to the schema
        "options": {"temperature": 0, "num_ctx": num_ctx, "num_predict": num_predict},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    started = time.monotonic()
    try:
        # Thinking models (e.g. qwen3) reason at length before answering, which is
        # slow on a CPU and not needed for extraction. Only models that support
        # thinking accept the flag, so ask Ollama first.
        if supports_thinking(http, host, model):
            request["think"] = False
        resp = http.post(f"{host}/api/chat", json=request)
    except httpx.ConnectError:
        return Completion(None, label, [
            f"could not reach Ollama at {host}; start the Ollama app, or use --no-llm"])
    except httpx.TimeoutException:
        return Completion(None, label, [
            f"{label} didn't finish within {OLLAMA_TIMEOUT_S / 60:.0f} minutes; "
            "try a smaller model, --llm-strategy staged, or Claude"], OLLAMA_TIMEOUT_S)
    finally:
        if own_client:
            http.close()
    elapsed = time.monotonic() - started

    if resp.status_code == 404:
        return Completion(None, label, [
            f"Ollama model '{model}' isn't installed; run: ollama pull {model}"], elapsed)
    if resp.is_error:
        return Completion(None, label, [
            f"Ollama request failed ({resp.status_code}): {resp.text[:200]}"], elapsed)
    body = resp.json()
    if body.get("done_reason") == "length":
        return Completion(None, label, [
            f"{label} hit its output or context limit before finishing "
            "(raise OLLAMA_NUM_PREDICT or OLLAMA_NUM_CTX)"], elapsed)
    try:
        data = schema.model_validate_json(body.get("message", {}).get("content", ""))
    except ValidationError:
        return Completion(None, label, [
            f"{label} returned output that didn't match the schema; skipped"], elapsed)
    return Completion(data, label, [], elapsed)


def supports_thinking(http: httpx.Client, host: str, model: str) -> bool:
    try:
        resp = http.post(f"{host}/api/show", json={"model": model}, timeout=10)
        return resp.status_code == 200 and "thinking" in (resp.json().get("capabilities") or [])
    except (httpx.HTTPError, ValueError):
        return False


def extract_with_ollama(company_name: str, pages: list,
                        http: httpx.Client | None = None) -> ExtractionResult:
    """Single-call extraction: all pages (up to the local budget) in one request (no caching)."""
    return extract_single(
        company_name, pages, lambda s, u, schema: complete(s, u, schema, http),
        OLLAMA_TOTAL_CHAR_LIMIT)
