"""Progress reporting for long-running lookups.

`research_company()` calls a `progress` callback with a ProgressEvent at each
stage. The CLI turns these into a spinner (people) or JSON lines (`--progress`,
for programs). A Python GUI can pass its own callback.

Stages, in order: resolve, companies_house, website_discovery, crawl, extract,
llm, save. Status is one of: started, done, skipped, failed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .schema import utcnow


@dataclass
class ProgressEvent:
    stage: str
    status: str
    message: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": "progress",
            "stage": self.stage,
            "status": self.status,
            "message": self.message,
            "data": self.data,
            "ts": utcnow().isoformat(),
        }


ProgressFn = Callable[[ProgressEvent], None]


def emit(progress: ProgressFn | None, stage: str, status: str,
         message: str | None = None, **data: Any) -> None:
    if progress is not None:
        progress(ProgressEvent(stage, status, message, data))
