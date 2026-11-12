"""Staged extraction, for local models with small context windows.

    material -> parts (each fits the model comfortably)
             -> one "notes" call per part (ChunkFindings)
             -> code combines products, sites and markets, de-duplicated
             -> one merge call writes summary, sectors, tags, business model

Each call has its own time limit, so a slow model can still finish a large
site. A failed part is skipped; a failed merge falls back to a summary built
from the notes, so finished work is never thrown away.

INFORMANT_STAGE_CHARS (default 9000) and INFORMANT_MAX_STAGES (default 5) tune
the part size and the maximum number of parts.
"""

from __future__ import annotations

import os

from ..progress import ProgressFn, emit
from .backends.base import CompleteFn
from .budget import fair_shares, remove_repeated_lines
from .prompts import (
    MERGE_SYSTEM_PROMPT,
    STAGE_SYSTEM_PROMPT,
    ChunkFindings,
    ExtractedOffice,
    ExtractedProduct,
    MergedProfile,
    WebsiteExtraction,
    merge_message,
    stage_message,
)
from .result import ExtractionResult, echoed, finish

DEFAULT_STAGE_CHARS = 9_000
DEFAULT_MAX_STAGES = 5
HEADER_ALLOWANCE = 300  # room for the "=== PAGE ..." header on each piece
MAX_FACTS = 40
MAX_ITEMS = 15

# Failures that will happen again on every part: stop instead of retrying.
_FATAL_HINTS = ("could not reach", "isn't installed", "key rejected", "extraction skipped")


def stage_settings() -> tuple[int, int]:
    chars = int(os.environ.get("INFORMANT_STAGE_CHARS", DEFAULT_STAGE_CHARS))
    stages = int(os.environ.get("INFORMANT_MAX_STAGES", DEFAULT_MAX_STAGES))
    return max(chars, 2_000), max(stages, 1)


def split_text(text: str, limit: int) -> list[str]:
    """Split on line boundaries into pieces of at most `limit` characters."""
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines():
        while len(line) > limit:  # a single enormous line: hard-cut it
            if current:
                pieces.append("\n".join(current))
                current, size = [], 0
            pieces.append(line[:limit])
            line = line[limit:]
        if size + len(line) + 1 > limit and current:
            pieces.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        pieces.append("\n".join(current))
    return [p for p in pieces if p.strip()]


def make_chunks(pages: list, stage_chars: int, max_stages: int) -> tuple[list[str], list[str]]:
    """Group page text into parts of at most `stage_chars`, at most `max_stages` of them.
    Every page gets a fair share of the total, so no page crowds out the others."""
    notes: list[str] = []
    texts = remove_repeated_lines(pages)
    original = sum(len(p.text) for p in pages)
    deduped = sum(len(t) for t in texts)
    if original - deduped >= 500:
        notes.append(f"removed {original - deduped:,} characters of text repeated across pages "
                     f"(menus, footers); {deduped:,} left")

    body_limit = stage_chars - HEADER_ALLOWANCE
    lengths = [len(t) if t.strip() else 0 for t in texts]

    def pack(budget: int) -> tuple[list[str], list[int]]:
        shares = fair_shares(lengths, budget, cap=budget)
        blocks: list[str] = []
        for page, text, share in zip(pages, texts, shares):
            if share == 0:
                continue
            for i, piece in enumerate(split_text(text[:share], body_limit)):
                more = " (continued)" if i else ""
                blocks.append(
                    f"=== PAGE ({page.kind}): {page.url}{more}\nTitle: {page.title or ''}\n\n{piece}")
        chunks: list[str] = []
        current = ""
        for block in blocks:
            if current and len(current) + 2 + len(block) > stage_chars:
                chunks.append(current)
                current = block
            else:
                current = f"{current}\n\n{block}" if current else block
        if current:
            chunks.append(current)
        return chunks, shares

    # Pieces from different pages don't always pack perfectly into parts. Rather than
    # drop the spill-over (which would cut whole pages), shrink every page's share a
    # little and repack until it fits.
    budget = body_limit * max_stages
    chunks, shares = pack(budget)
    while len(chunks) > max_stages and budget > 1_000:
        budget = int(budget * 0.9)
        chunks, shares = pack(budget)
    chunks = chunks[:max_stages]

    for page, text, share in zip(pages, texts, shares):
        if 0 < share < len(text):
            notes.append(f"{page.url}: sent first {share:,} of {len(text):,} characters to the LLM")
    return chunks, notes


def needs_staging(pages: list, single_limit: int) -> bool:
    return sum(len(t) for t in remove_repeated_lines(pages)) > single_limit


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def _dedupe(items: list, key) -> list:
    seen: set[str] = set()
    out = []
    for item in items:
        k = key(item)
        if k and k not in seen:
            seen.add(k)
            out.append(item)
    return out


def extract_staged(company_name: str, pages: list, complete: CompleteFn,
                   progress: ProgressFn | None = None) -> ExtractionResult:
    stage_chars, max_stages = stage_settings()
    chunks, notes = make_chunks(pages, stage_chars, max_stages)
    if not chunks:
        return ExtractionResult(None, notes=["no page text to send to the LLM"], strategy="staged")

    parts = len(chunks)
    facts: list[str] = []
    products: list[ExtractedProduct] = []
    offices: list[ExtractedOffice] = []
    markets: list[str] = []
    model: str | None = None
    elapsed = 0.0
    calls = 0

    for i, chunk in enumerate(chunks, 1):
        emit(progress, "llm", "progress", f"Reading part {i} of {parts}", part=i, parts=parts)
        c = complete(STAGE_SYSTEM_PROMPT, stage_message(company_name, chunk, i, parts), ChunkFindings)
        calls += 1
        model = c.model or model
        elapsed += c.elapsed
        if c.data is None:
            reason = c.notes[-1] if c.notes else "no result"
            notes.append(f"part {i} of {parts} failed: {reason}")
            if any(h in reason for h in _FATAL_HINTS):
                return ExtractionResult(None, model=model, notes=notes, strategy="staged")
            continue
        f: ChunkFindings = c.data
        good = [x.strip() for x in f.key_facts if x.strip() and not echoed(x)]
        if f.key_facts and len(good) * 2 < len(f.key_facts):
            notes.append(f"part {i} of {parts}: {model} copied the prompt instead of taking notes; ignored")
            continue
        facts += good
        products += [p for p in f.products if p.name.strip() and not echoed(p.name)]
        offices += [o for o in f.offices if o.label.strip() and not echoed(o.label)]
        markets += [m.strip() for m in f.target_markets if m.strip() and not echoed(m)]

    facts = _dedupe(facts, _norm)
    if len(facts) > MAX_FACTS:
        notes.append(f"kept {MAX_FACTS} of {len(facts)} notes for the merge step")
        facts = facts[:MAX_FACTS]
    products = _dedupe(products, lambda p: _norm(p.name))[:MAX_ITEMS]
    offices = _dedupe(offices, lambda o: _norm(f"{o.label} {o.address}"))[:MAX_ITEMS]
    markets = _dedupe(markets, _norm)

    if not facts and not products:
        return ExtractionResult(None, model=model, notes=notes + ["no findings from any part"],
                                strategy="staged")

    emit(progress, "llm", "progress", "Combining notes", part=parts, parts=parts, merging=True)
    m = complete(MERGE_SYSTEM_PROMPT,
                 merge_message(company_name, facts, [p.name for p in products], markets),
                 MergedProfile)
    calls += 1
    elapsed += m.elapsed
    merged: MergedProfile | None = m.data
    if merged is None or echoed(merged.summary):
        reason = (m.notes[-1] if m.notes else "copied the prompt") if merged is None else "copied the prompt"
        notes.append(f"merge step failed ({reason}); summary built from the notes instead")
        merged = MergedProfile(summary=" ".join(facts[:3]), business_model="unknown",
                               sectors=["Other"], tags=[], target_markets=markets)

    notes.append(f"staged extraction: {parts} part(s) + merge, {calls} calls"
                 + (f", {elapsed:.0f}s" if elapsed else ""))
    data = WebsiteExtraction(
        summary=merged.summary,
        business_model=merged.business_model,
        sectors=merged.sectors,
        tags=merged.tags,
        target_markets=merged.target_markets or markets,
        products=products,
        offices=offices,
    )
    result = finish(data, model, notes)
    result.strategy = "staged"
    return result
