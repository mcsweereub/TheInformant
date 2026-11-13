"""Staged extraction: parts -> notes per part -> merge, tested with a fake Ollama."""

import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from informant import cli
from informant.llm import choose_strategy, extract_with_llm, make_chunks
from informant.llm.staged import split_text
from informant.progress import ProgressEvent
from informant.sources.website.parse import Page

OLLAMA = "http://localhost:11434/api/chat"
OLLAMA_SHOW = "http://localhost:11434/api/show"


def page(kind: str, path: str, text: str) -> Page:
    return Page(url=f"https://acme.example{path}", kind=kind, status_code=200,
                title=kind.title(), text=text)


def filler(label: str, chars: int) -> str:
    """Distinct lines (so repeated-line removal keeps them), about `chars` long."""
    lines, size, i = [], 0, 0
    while size < chars:
        line = f"{label} fact number {i}: Acme makes industrial widgets like Widget Pro in London."
        lines.append(line)
        size += len(line) + 1
        i += 1
    return "\n".join(lines)


INVESTOR_FACTS = ("Acme was founded in Leeds in 1951. In 2021 Acme raised £5 million in a Series A "
                  "led by Northern Ventures, which now owns 20% of Acme.")
SMALL = [page("home", "/", filler("Home", 2_000)), page("about", "/about", filler("About", 2_000))]
LARGE = [page("home", "/", filler("Home", 14_000)),
         page("products", "/products", filler("Products", 14_000)),
         page("investors", "/investors", filler("Investors", 3_000) + "\n" + INVESTOR_FACTS)]

STAGE_ANSWER = {
    "key_facts": ["Acme makes industrial widgets.", "Acme sells to UK factories."],
    "products": [{"name": "Widget Pro", "description": "A strong widget.", "category": "widgets",
                  "source_page": "https://acme.example/products"}],
    "offices": [{"label": "Head office", "address": "1 High St", "locality": "London",
                 "country": "United Kingdom", "source_page": "https://acme.example/"}],
    "target_markets": ["UK manufacturers"],
    "milestones": [
        {"year": "1951", "event": "Acme was founded in Leeds", "source_page": "https://acme.example/investors"},
        {"year": "2021", "event": "Acme raised a Series A", "source_page": "https://acme.example/investors"},
        {"year": "1999", "event": "Acme opened a moon base", "source_page": "https://acme.example/"},  # invented
    ],
    "funding_rounds": [{"year": "2021", "round": "Series A", "amount": "£5 million",
                        "investors": ["Northern Ventures", "Imaginary Capital"],
                        "source_page": "https://acme.example/investors"}],
    "shareholders": [{"name": "Northern Ventures", "stake": "20%",
                      "source_page": "https://acme.example/investors"}],
}
MERGE_ANSWER = {
    "summary": "Acme makes industrial widgets for UK factories.",
    "business_model": "B2B",
    "sectors": ["Engineering & Manufacturing"],
    "tags": ["widgets", "industrial"],
    "target_markets": ["UK manufacturers"],
}
SINGLE_ANSWER = dict(MERGE_ANSWER, products=STAGE_ANSWER["products"], offices=STAGE_ANSWER["offices"],
                     key_facts=STAGE_ANSWER["key_facts"], milestones=[], funding_rounds=[],
                     shareholders=[])


class FakeOllama:
    """Answers by the schema each request asks for, and records what it was sent."""

    def __init__(self, stage=None, merge=None, fail_parts=()):
        self.stage = stage or STAGE_ANSWER
        self.merge = merge if merge is not None else MERGE_ANSWER
        self.fail_parts = set(fail_parts)
        self.calls: list[tuple[str, str]] = []  # (schema title, user message)
        respx.post(OLLAMA_SHOW).respond(json={"capabilities": ["completion"]})
        respx.post(OLLAMA).mock(side_effect=self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        title = body["format"]["title"]
        self.calls.append((title, body["messages"][1]["content"]))
        if title == "ChunkFindings":
            part = sum(1 for t, _ in self.calls if t == "ChunkFindings")
            if part in self.fail_parts:
                return httpx.Response(200, json={"message": {"content": "{broken"}, "done_reason": "stop"})
            answer = self.stage
        elif title == "MergedProfile":
            if self.merge == "fail":
                return httpx.Response(500, text="boom")
            answer = self.merge
        else:
            answer = SINGLE_ANSWER
        return httpx.Response(200, json={"message": {"content": json.dumps(answer)}, "done_reason": "stop"})

    def titles(self) -> list[str]:
        return [t for t, _ in self.calls]


@pytest.fixture(autouse=True)
def ollama_env(monkeypatch):
    monkeypatch.setenv("INFORMANT_LLM", "ollama")
    for k in ("INFORMANT_LLM_STRATEGY", "INFORMANT_STAGE_CHARS", "INFORMANT_MAX_STAGES",
              "OLLAMA_MODEL", "OLLAMA_HOST"):
        monkeypatch.delenv(k, raising=False)


def test_split_text_respects_limit_and_keeps_everything():
    text = filler("Line", 5_000) + "\n" + "x" * 2_500
    pieces = split_text(text, 1_000)
    assert all(len(p) <= 1_000 for p in pieces)
    assert "".join(pieces).replace("\n", "") == text.replace("\n", "")


def test_make_chunks_fits_limits_and_includes_every_page():
    chunks, notes = make_chunks(LARGE, stage_chars=9_000, max_stages=5)
    assert 1 < len(chunks) <= 5 and all(len(c) <= 9_000 for c in chunks)
    joined = "\n".join(chunks)
    for kind in ("home", "products", "investors"):
        assert f"=== PAGE ({kind})" in joined
    assert "(continued)" in joined  # a long page spans parts


def test_make_chunks_shares_space_when_over_budget():
    chunks, notes = make_chunks(LARGE, stage_chars=4_000, max_stages=2)
    assert len(chunks) <= 2
    assert "=== PAGE (investors)" in "\n".join(chunks)  # small page not crowded out
    assert any("sent first" in n for n in notes)


def test_auto_strategy():
    assert choose_strategy("ollama", SMALL) == "single"
    assert choose_strategy("ollama", LARGE) == "staged"
    assert choose_strategy("claude", LARGE) == "single"  # big context, fewer calls
    assert choose_strategy("claude", LARGE, "staged") == "staged"
    assert choose_strategy("ollama", LARGE, "single") == "single"


@respx.mock
def test_small_site_uses_one_call():
    fake = FakeOllama()
    result = extract_with_llm("Acme", SMALL)
    assert fake.titles() == ["WebsiteExtraction"]
    assert result.strategy == "single" and result.data.summary.startswith("Acme makes")


@respx.mock
def test_large_site_is_staged_then_merged():
    fake = FakeOllama()
    events: list[ProgressEvent] = []
    result = extract_with_llm("Acme", LARGE, progress=events.append)

    titles = fake.titles()
    parts = titles.count("ChunkFindings")
    assert parts >= 2 and titles[-1] == "MergedProfile" and titles.count("MergedProfile") == 1
    assert all(len(msg) < 10_000 for _, msg in fake.calls)  # every call stays small

    # The merge call sees the notes, not the raw pages.
    merge_input = fake.calls[-1][1]
    assert "- Acme makes industrial widgets." in merge_input and "Widget Pro" in merge_input
    assert "fact number" not in merge_input

    d = result.data
    assert result.strategy == "staged" and result.model == "ollama/qwen3:4b"
    assert d.summary == MERGE_ANSWER["summary"] and d.sectors == ["Engineering & Manufacturing"]
    assert [p.name for p in d.products] == ["Widget Pro"]  # same product from each part, once
    assert len(d.offices) == 1
    assert any("staged extraction" in n for n in result.notes)

    progress = [(e.stage, e.status, e.data.get("part")) for e in events]
    assert progress[0] == ("llm", "progress", 1)
    assert events[-1].message == "Combining notes"


@respx.mock
def test_failed_part_is_skipped_and_rest_still_used():
    fake = FakeOllama(fail_parts={1})
    result = extract_with_llm("Acme", LARGE)
    assert result.data is not None and result.data.summary == MERGE_ANSWER["summary"]
    assert any("part 1 of" in n and "failed" in n for n in result.notes)
    assert fake.titles()[-1] == "MergedProfile"


@respx.mock
def test_failed_merge_falls_back_to_notes():
    FakeOllama(merge="fail")
    result = extract_with_llm("Acme", LARGE)
    d = result.data
    assert d is not None
    assert d.summary == "Acme makes industrial widgets. Acme sells to UK factories."
    assert d.business_model == "unknown" and d.sectors == ["Other"]
    assert [p.name for p in d.products] == ["Widget Pro"]  # finished work is kept
    assert any("merge step failed" in n for n in result.notes)


@respx.mock
def test_echoed_notes_are_ignored():
    echo = dict(STAGE_ANSWER, key_facts=[
        "short, self-contained sentences about what the company does",
        "who it sells to, where it operates, how big it is, its partners"])
    FakeOllama(stage=echo)
    result = extract_with_llm("Acme", LARGE)
    assert result.data is None
    assert any("copied the prompt" in n for n in result.notes)
    assert result.notes[-1] == "no findings from any part"


@respx.mock
def test_ollama_down_stops_after_first_part():
    respx.post(OLLAMA_SHOW).mock(side_effect=httpx.ConnectError("refused"))
    route = respx.post(OLLAMA).mock(side_effect=httpx.ConnectError("refused"))
    result = extract_with_llm("Acme", LARGE)
    assert result.data is None and route.call_count == 1
    assert "start the Ollama app" in result.notes[-1]


@respx.mock
def test_forced_staged_on_small_site_and_stage_settings(monkeypatch):
    monkeypatch.setenv("INFORMANT_STAGE_CHARS", "3000")
    monkeypatch.setenv("INFORMANT_MAX_STAGES", "2")
    fake = FakeOllama()
    result = extract_with_llm("Acme", SMALL, strategy="staged")
    assert fake.titles() == ["ChunkFindings", "ChunkFindings", "MergedProfile"]
    assert result.strategy == "staged"


def test_cli_rejects_bad_strategy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.app, ["lookup", "01234567", "--json", "--llm-strategy", "fast"])
    assert result.exit_code == 3
    assert json.loads(result.stdout)["error"]["code"] == "config_error"


@respx.mock
def test_history_funding_and_shareholders_are_collected_and_checked():
    FakeOllama()
    d = extract_with_llm("Acme", LARGE).data
    # Every part returns the same milestones: de-duplicated, sorted, invented one dropped.
    assert [(m.year, m.event) for m in d.milestones] == [
        ("1951", "Acme was founded in Leeds"), ("2021", "Acme raised a Series A")]
    assert len(d.funding_rounds) == 1
    r = d.funding_rounds[0]
    assert (r.year, r.round, r.amount, r.investors) == ("2021", "Series A", "£5 million", ["Northern Ventures"])
    assert [(s.name, s.stake) for s in d.shareholders] == [("Northern Ventures", "20%")]
    # "Acme sells to UK factories." isn't supported by the page text, so it's dropped.
    assert d.key_facts == ["Acme makes industrial widgets."]
