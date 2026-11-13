"""Checks applied to every LLM answer: fixed vocabularies in the schema, tolerant
parsing, output cap, and grounding against the text the model was given."""

import json

import respx

from informant.llm.backends.ollama import complete
from informant.llm.prompts import (
    BUSINESS_MODELS,
    SECTORS,
    SYSTEM_PROMPT,
    ChunkFindings,
    ExtractedOffice,
    ExtractedProduct,
    MergedProfile,
    WebsiteExtraction,
)
from informant.llm.result import Grounding, finish, merge_duplicate_offices

OLLAMA = "http://localhost:11434/api/chat"
OLLAMA_SHOW = "http://localhost:11434/api/show"


def extraction(**over) -> WebsiteExtraction:
    base = dict(summary="Planlogic provides ETRM software for LNG trading.", business_model="B2B",
                sectors=["Energy & Utilities"], tags=[], target_markets=[], products=[], offices=[])
    base.update(over)
    return WebsiteExtraction(**base)


def product(name: str) -> ExtractedProduct:
    return ExtractedProduct(name=name, description="x", category="x", source_page="https://x/")


def office(label: str, address: str = "", locality: str = "") -> ExtractedOffice:
    return ExtractedOffice(label=label, address=address, locality=locality, country="UK",
                           source_page="https://x/")


# --- schema ----------------------------------------------------------------------------


def test_schema_states_fixed_lists_and_limits():
    schema = WebsiteExtraction.model_json_schema()
    props = schema["properties"]
    assert props["business_model"]["enum"] == list(BUSINESS_MODELS)
    assert props["sectors"]["items"]["enum"] == SECTORS and props["sectors"]["maxItems"] == 3
    assert props["tags"]["maxItems"] == 10 and props["products"]["maxItems"] == 15
    assert ChunkFindings.model_json_schema()["properties"]["key_facts"]["maxItems"] == 12
    assert MergedProfile.model_json_schema()["properties"]["business_model"]["enum"]
    assert "B2C if it sells mainly to individual consumers" in SYSTEM_PROMPT


def test_parsing_is_tolerant_when_a_backend_ignores_the_schema():
    d = extraction(business_model="b2c", sectors=["Insurance", "Invented Sector", "Other"],
                   tags=[f"tag{i}" for i in range(14)])
    assert d.business_model == "B2C" and d.sectors == ["Insurance", "Other"] and len(d.tags) == 10
    assert extraction(business_model="exactly one of B2B, B2C").business_model == "unknown"


@respx.mock
def test_ollama_output_is_capped(monkeypatch):
    monkeypatch.delenv("OLLAMA_NUM_PREDICT", raising=False)
    respx.post(OLLAMA_SHOW).respond(json={"capabilities": ["completion"]})
    route = respx.post(OLLAMA).respond(json={"message": {"content": ""}, "done_reason": "length"})
    c = complete("sys", "user", MergedProfile)
    assert json.loads(route.calls[0].request.content)["options"]["num_predict"] == 4096
    assert c.data is None and "OLLAMA_NUM_PREDICT" in c.notes[-1]


# --- grounding -------------------------------------------------------------------------

LNG_SITE = """Planlogic EOS is the ETRM of choice for LNG trading specialists.
Energy Trading and Risk Management software, co-developed with leading portfolio companies.
Our customers include global energy traders. Case studies. Bakeries and bakers love it.
Contact us: 20-21 Jockey's Fields, London WC1R 4BW. We also have a Cardiff office."""


def test_word_matching_allows_simple_endings():
    g = Grounding(LNG_SITE)
    assert g.has("lng") and not g.has("lpg")
    assert g.has("trader") and g.has("bakery") and g.has("customer")
    assert not g.has("mortgage")
    assert g.share("LNG trading") == 1.0 and g.share("LPG trading software") < 1.0


def test_unsupported_tags_products_and_sites_are_dropped_with_notes():
    data = extraction(
        tags=["etrm", "lng", "lpg", "lpg trading software", "energy trading", "b2b", "saas"],
        products=[product("EOS"), product("EOS ETRM platform"), product("Mortgage Advice Hub")],
        offices=[office("Head office", "20-21 Jockey's Fields", "London"),
                 office("Paris office", "12 Rue Inventee", "Paris")])
    notes: list[str] = []
    result = finish(data, "ollama/test", notes, source_text=LNG_SITE)

    d = result.data
    assert d.tags == ["b2b", "energy trading", "etrm", "lng", "saas"]  # generic tags may stay
    assert [p.name for p in d.products] == ["EOS", "EOS ETRM platform"]
    assert [o.locality for o in d.offices] == ["London"]
    joined = " | ".join(result.notes)
    assert "lpg, lpg trading software" in joined
    assert "Mortgage Advice Hub" in joined and "Paris office" in joined


def test_without_source_text_nothing_is_dropped():
    data = extraction(tags=["anything"], products=[product("Whatever")])
    result = finish(data, "m", [])
    assert result.data.tags == ["anything"] and len(result.data.products) == 1


def test_duplicate_offices_are_merged_per_locality():
    merged = merge_duplicate_offices([
        office("London", "London", "London"),
        office("London office", "3 minute walk from Liverpool Street station", "London"),
        office("Cardiff", "Cardiff", "Cardiff"),
        office("Cardiff office", "Cardiff", "Cardiff"),
        office("Leeds store", "1 Briggate", "Leeds"),
        office("Leeds depot", "9 Canal Road", "Leeds"),
        office("Dublin", "", "Dublin"),
    ])
    assert [(o.label, o.locality) for o in merged] == [
        ("London office", "London"),  # the entry with a real address wins
        ("Cardiff", "Cardiff"),  # neither has one: keep the first
        ("Leeds store", "Leeds"), ("Leeds depot", "Leeds"),  # two real addresses: both stay
        ("Dublin", "Dublin"),
    ]


def test_thin_tags_are_topped_up_from_terms_the_pages_repeat():
    text = "\n".join([
        "Planlogic EOS is the ETRM of choice for LNG trading specialists.",
        "Energy trading and risk management for LNG cargoes. EOS handles LNG logistics.",
        "Leaders in ETRM innovation. Energy trading companies choose EOS.",
        "Risk management, energy trading and LNG scheduling in one ETRM system.",
        "Planlogic Software is trusted. Planlogic Software delivers. Planlogic Software cares.",
        "=== PAGE (about): https://planlogic.example/about",
        "Title: About PAGE PAGE PAGE",
        "Our risk management tools: see https://planlogic.example/eos https://planlogic.example/x",
    ])
    data = extraction(tags=["etrm", "lpg", "lpg trading"])
    result = finish(data, "ollama/test", [], source_text=text, company_name="PLANLOGIC SOFTWARE LIMITED")
    tags = result.data.tags
    assert "lpg" not in tags and "etrm" in tags
    assert {"lng", "eos", "energy trading", "risk management"} <= set(tags)
    assert "planlogic software" not in tags  # the company's own name isn't a useful tag
    assert any("added tag(s) from terms the pages repeat" in n for n in result.notes)

    enough = extraction(tags=["etrm", "lng", "energy trading"])
    assert finish(enough, "m", [], source_text=text).data.tags == ["energy trading", "etrm", "lng"]
