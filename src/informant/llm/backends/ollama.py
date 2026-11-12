"""Ollama (local model, free). Needs Ollama running (https://ollama.com).
OLLAMA_MODEL, OLLAMA_HOST and OLLAMA_NUM_CTX override the defaults."""

from __future__ import annotations

import os
import time

import httpx
from pydantic import ValidationError

from ..budget import OLLAMA_TOTAL_CHAR_LIMIT, build_page_text
from ..models import DEFAULT_OLLAMA_MODEL, ollama_host
from ..prompts import SYSTEM_PROMPT, WebsiteExtraction, user_message
from ..result import ExtractionResult, finish

DEFAULT_OLLAMA_NUM_CTX = 16384  # tokens; Ollama's own default is too small for web pages
OLLAMA_TIMEOUT_S = 1200.0  # 20 min: local models on a CPU are slow with long input


def extract_with_ollama(company_name: str, pages: list,
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
            {"role": "user", "content": user_message(company_name, page_text)},
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
    return finish(data, label, notes)


def supports_thinking(http: httpx.Client, host: str, model: str) -> bool:
    try:
        resp = http.post(f"{host}/api/show", json={"model": model}, timeout=10)
        return resp.status_code == 200 and "thinking" in (resp.json().get("capabilities") or [])
    except (httpx.HTTPError, ValueError):
        return False
