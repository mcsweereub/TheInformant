"""Fitting page text into the model's input: drop repeated lines, then give each
page a fair share of the character budget."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid importing the website package at runtime (import cycle)
    from ..sources.website.parse import Page

PAGE_CHAR_LIMIT = 12_000
TOTAL_CHAR_LIMIT = 60_000  # Claude
OLLAMA_TOTAL_CHAR_LIMIT = 28_000  # ~7k tokens, leaves room in the context for prompt + output


def remove_repeated_lines(pages: list[Page]) -> list[str]:
    """Each page's text with any line already seen (on this or an earlier page)
    removed. Menus, cookie banners and footers repeat on every page; the model
    only needs them once. Pages are in crawl order, so the homepage keeps its copy."""
    seen: set[str] = set()
    texts = []
    for page in pages:
        kept = []
        for line in page.text.splitlines():
            key = " ".join(line.lower().split())
            if not key or key in seen:
                continue
            seen.add(key)
            kept.append(line)
        texts.append("\n".join(kept))
    return texts


def fair_shares(lengths: list[int], budget: int, cap: int = PAGE_CHAR_LIMIT) -> list[int]:
    """Split `budget` characters between pages so each gets an equal share, up to
    `cap`; pages needing less than their share pass the spare on to the others.
    This way a long page (e.g. legal text) can't crowd out later ones."""
    shares = [0] * len(lengths)
    remaining = budget
    pending = [i for i, n in enumerate(lengths) if n > 0]
    # Smallest pages first: they take what they need, the rest split what's left.
    for pos, i in enumerate(sorted(pending, key=lambda i: lengths[i])):
        fair = remaining // (len(pending) - pos)
        shares[i] = min(lengths[i], cap, fair)
        remaining -= shares[i]
    return shares


def build_page_text(pages: list[Page], total_limit: int = TOTAL_CHAR_LIMIT) -> tuple[str, list[str]]:
    notes: list[str] = []
    texts = remove_repeated_lines(pages)
    original = sum(len(p.text) for p in pages)
    deduped = sum(len(t) for t in texts)
    if original - deduped >= 500:
        notes.append(f"removed {original - deduped:,} characters of text repeated across pages "
                     f"(menus, footers); {deduped:,} left")

    shares = fair_shares([len(t) if t.strip() else 0 for t in texts], total_limit)
    parts: list[str] = []
    for page, text, share in zip(pages, texts, shares):
        if share == 0:
            continue  # nothing on this page that wasn't already sent
        if len(text) > share:
            notes.append(f"{page.url}: sent first {share:,} of {len(text):,} characters to the LLM")
            text = text[:share]
        parts.append(f"=== PAGE ({page.kind}): {page.url}\nTitle: {page.title or ''}\n\n{text}")
    return "\n\n".join(parts), notes
