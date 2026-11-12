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


# complete(system_prompt, user_message, schema) -> Completion
CompleteFn = Callable[[str, str, type[BaseModel]], Completion]
