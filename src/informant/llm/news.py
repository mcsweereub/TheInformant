"""Judging news headlines with the LLM: is each one about this company, what is
its tone, and what kind of story is it?

One small call covers every headline. Each answer is a choice from a fixed list,
which small local models handle reliably. Only headlines are sent, never articles.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from .backends.base import CompleteFn

Sentiment = Literal["negative", "neutral", "positive"]
Topic = Literal[
    "results_and_finances", "funding_or_deal", "product_or_launch", "leadership_and_people",
    "legal_or_regulatory", "expansion_or_closure", "customer_or_reputation", "other",
]
SENTIMENT_SCORES = {"negative": -1.0, "neutral": 0.0, "positive": 1.0}
MAX_HEADLINES = 30

NEWS_SYSTEM_PROMPT = """You judge news headlines for a personal research database about companies. You reply with JSON only.

For each numbered headline, give:
about_company: true if the named company is one of the main subjects of the story. That includes stories where another firm is buying, investing in, suing, partnering with or competing against it. False only if the company is mentioned in passing, or the name belongs to something else (a person, an animal, a different firm).
sentiment: how the story reflects on the company: negative, neutral or positive. A crime committed against the company, or a routine announcement, is neutral.
topic: the kind of story: results_and_finances, funding_or_deal (investment, takeover, merger, sale), product_or_launch, leadership_and_people, legal_or_regulatory, expansion_or_closure (sites opening or closing, jobs created or cut), customer_or_reputation, or other.

Judge only from the headline text. Give exactly one answer per headline, using its number."""


class HeadlineJudgement(BaseModel):
    n: int
    about_company: bool
    sentiment: Sentiment
    topic: Topic


class NewsJudgements(BaseModel):
    items: Annotated[list[HeadlineJudgement], Field(max_length=MAX_HEADLINES)]


def judge_headlines(company_name: str, what_it_does: str | None, headlines: list[str],
                    complete: CompleteFn) -> tuple[dict[int, HeadlineJudgement], list[str]]:
    """Returns ({headline index: judgement}, notes). Headlines the model skips
    are simply absent from the result."""
    headlines = headlines[:MAX_HEADLINES]
    if not headlines:
        return {}, []
    numbered = "\n".join(f"{i}. {h}" for i, h in enumerate(headlines, 1))
    context = f"\nWhat it does: {what_it_does}" if what_it_does else ""
    user = (f"Company: {company_name}{context}\n\nHeadlines:\n{numbered}\n\n"
            f"=== END OF HEADLINES\n\nNow judge each of the {len(headlines)} headlines above.")
    c = complete(NEWS_SYSTEM_PROMPT, user, NewsJudgements)
    if c.data is None:
        return {}, [f"headlines not judged: {c.notes[-1] if c.notes else 'no result'}"]
    judged = {j.n - 1: j for j in c.data.items if 1 <= j.n <= len(headlines)}
    notes = []
    if c.cached:
        notes.append("headline judgements reused from the local cache")
    elif c.model and c.model.startswith("ollama/"):
        notes.append(f"{c.model} judged {len(judged)} headlines in {c.elapsed:.0f}s")
    return judged, notes
