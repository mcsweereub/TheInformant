"""Claude (Anthropic API, paid). Needs ANTHROPIC_API_KEY.
INFORMANT_MODEL / INFORMANT_EFFORT override model and effort."""

from __future__ import annotations

import os
import time

import anthropic
from pydantic import BaseModel

from ..budget import TOTAL_CHAR_LIMIT
from ..models import DEFAULT_MODEL
from ..result import ExtractionResult
from ..single import extract_single
from .base import Completion

DEFAULT_EFFORT = "low"  # extraction from supplied text is a simple task


def complete(system: str, user: str, schema: type[BaseModel],
             client: anthropic.Anthropic | None = None) -> Completion:
    """One structured call: returns an instance of `schema`, or None with a reason."""
    model = os.environ.get("INFORMANT_MODEL", DEFAULT_MODEL)
    effort = os.environ.get("INFORMANT_EFFORT", DEFAULT_EFFORT)
    started = time.monotonic()
    try:
        client = client or anthropic.Anthropic()
        response = client.beta.messages.parse(
            model=model,
            max_tokens=16000,
            # If a safety classifier declines, the API retries on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": effort},
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=schema,
        )
    except anthropic.AuthenticationError:
        return Completion(None, model, ["Anthropic API key rejected; LLM extraction skipped"])
    except anthropic.APIStatusError as e:
        return Completion(None, model, [f"LLM request failed ({e.status_code}): {e.message}"])
    except anthropic.APIConnectionError:
        return Completion(None, model, ["could not reach the Anthropic API; LLM extraction skipped"])
    except anthropic.AnthropicError as e:  # e.g. no credentials configured
        return Completion(None, model, [f"LLM extraction skipped: {e}"])

    elapsed = time.monotonic() - started
    if response.stop_reason == "refusal":
        return Completion(None, response.model, ["LLM declined this request"], elapsed)
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        return Completion(None, response.model, ["LLM output was incomplete; skipped"], elapsed)
    return Completion(response.parsed_output, response.model, [], elapsed)


def extract_with_claude(company_name: str, pages: list,
                        client: anthropic.Anthropic | None = None) -> ExtractionResult:
    """Single-call extraction: all pages in one request (no caching)."""
    return extract_single(
        company_name, pages, lambda s, u, schema: complete(s, u, schema, client), TOTAL_CHAR_LIMIT)
