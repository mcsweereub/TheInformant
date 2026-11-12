"""Claude (Anthropic API, paid). Needs ANTHROPIC_API_KEY.
INFORMANT_MODEL / INFORMANT_EFFORT override model and effort."""

from __future__ import annotations

import os

import anthropic

from ..budget import TOTAL_CHAR_LIMIT, build_page_text
from ..models import DEFAULT_MODEL
from ..prompts import SYSTEM_PROMPT, WebsiteExtraction, user_message
from ..result import ExtractionResult, finish

DEFAULT_EFFORT = "low"  # extraction from supplied text is a simple task


def extract_with_claude(company_name: str, pages: list,
                        client: anthropic.Anthropic | None = None) -> ExtractionResult:
    model = os.environ.get("INFORMANT_MODEL", DEFAULT_MODEL)
    effort = os.environ.get("INFORMANT_EFFORT", DEFAULT_EFFORT)
    page_text, notes = build_page_text(pages, total_limit=TOTAL_CHAR_LIMIT)
    if not page_text:
        return ExtractionResult(None, notes=["no page text to send to the LLM"])

    try:
        client = client or anthropic.Anthropic()
        response = client.beta.messages.parse(
            model=model,
            max_tokens=16000,
            # If a safety classifier declines, the API retries on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": effort},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message(company_name, page_text)}],
            output_format=WebsiteExtraction,
        )
    except anthropic.AuthenticationError:
        return ExtractionResult(None, notes=notes + ["Anthropic API key rejected; LLM extraction skipped"])
    except anthropic.APIStatusError as e:
        return ExtractionResult(None, notes=notes + [f"LLM request failed ({e.status_code}): {e.message}"])
    except anthropic.APIConnectionError:
        return ExtractionResult(None, notes=notes + ["could not reach the Anthropic API; LLM extraction skipped"])
    except anthropic.AnthropicError as e:  # e.g. no credentials configured
        return ExtractionResult(None, notes=notes + [f"LLM extraction skipped: {e}"])

    if response.stop_reason == "refusal":
        return ExtractionResult(None, model=response.model, notes=notes + ["LLM declined this request"])
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        return ExtractionResult(None, model=response.model, notes=notes + ["LLM output was incomplete; skipped"])

    return finish(response.parsed_output, response.model, notes)
