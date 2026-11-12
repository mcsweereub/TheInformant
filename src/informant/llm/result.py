"""The outcome of an extraction, plus the checks and tidying applied to every
model's answer before it's trusted."""

from __future__ import annotations

from dataclasses import dataclass, field

from .prompts import SECTORS, SYSTEM_PROMPT, WebsiteExtraction


@dataclass
class ExtractionResult:
    data: WebsiteExtraction | None
    model: str | None = None
    notes: list[str] = field(default_factory=list)


def copied_instructions(data: WebsiteExtraction) -> bool:
    """True if the model echoed prompt wording instead of extracting facts
    (small local models sometimes fill fields with the instructions)."""
    prompt = SYSTEM_PROMPT.lower()
    placeholders = {"product name", "short category", "one-sentence description",
                    "office name", "city or town", "country"}

    def echoed(value: str) -> bool:
        v = value.strip().lower().rstrip(".")
        return v in placeholders or (len(v) > 25 and v in prompt)

    if echoed(data.summary) or echoed(data.business_model):
        return True
    values = [p.name for p in data.products] + [p.description for p in data.products]
    values += data.target_markets + [o.label for o in data.offices]
    return bool(values) and sum(echoed(v) for v in values) >= max(1, len(values) // 3)


def clean(data: WebsiteExtraction) -> WebsiteExtraction:
    data.sectors = list(dict.fromkeys(s for s in data.sectors if s in SECTORS)) or ["Other"]
    data.tags = sorted({t.strip().lower() for t in data.tags if t.strip()})
    return data


def finish(data: WebsiteExtraction, model: str | None, notes: list[str]) -> ExtractionResult:
    """Reject echoed prompts, tidy the rest."""
    if copied_instructions(data):
        return ExtractionResult(None, model=model, notes=notes + [
            f"{model} copied the prompt instead of extracting facts; result discarded "
            "(try a larger model)"])
    return ExtractionResult(clean(data), model=model, notes=notes)
