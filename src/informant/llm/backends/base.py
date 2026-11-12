"""What every backend returns from a single model call."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel


@dataclass
class Completion:
    data: Any | None  # an instance of the requested schema, or None on failure
    model: str | None  # e.g. "claude-haiku-5-5" or "ollama/qwen3:4b"
    notes: list[str] = field(default_factory=list)  # why it failed, if it did
    elapsed: float = 0.0  # seconds
    cached: bool = False  # answered from the local cache, no model call made


# complete(system_prompt, user_message, schema) -> Completion
CompleteFn = Callable[[str, str, type[BaseModel]], Completion]


def with_cache(complete: CompleteFn, cache, model_id: str) -> CompleteFn:
    """Wrap a backend's complete() so identical requests are answered from `cache`.

    The key covers the model, both prompts and the schema, so changing any of
    them (a new model, an edited prompt, different page text) is a fresh call.
    Only successful answers are stored; failures are always retried.
    """
    from pydantic import ValidationError

    from ...cache import llm_key
    from ...schema import utcnow

    def cached_complete(system: str, user: str, schema: type[BaseModel]) -> Completion:
        key = llm_key(model_id, system, user, schema)
        hit = cache.get(key)
        if hit is not None:
            try:
                return Completion(schema.model_validate(hit["data"]), hit.get("model"), [], 0.0,
                                  cached=True)
            except (ValidationError, KeyError, TypeError):
                pass  # damaged or outdated entry: fall through to a real call
        c = complete(system, user, schema)
        if c.data is not None:
            cache.put(key, {"model": c.model, "model_id": model_id, "schema": schema.__name__,
                            "elapsed": round(c.elapsed, 1), "created_at": utcnow().isoformat(),
                            "data": c.data.model_dump(mode="json")})
        return c

    return cached_complete
