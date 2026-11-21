"""The relationship graph built from a folder of saved profiles."""

import json

from typer.testing import CliRunner

from informant.cli import app
from informant.graph import build_graph, connections, find_node, neighbours, save_graph
from informant.schema import (
    CoOwner,
    CompanyProfile,
    Customer,
    Funding,
    FundingRound,
    Identity,
    LegalEntity,
    LeiEntity,
    Ownership,
    ParentCompany,
    PoliticalDonation,
    PoliticalDonations,
    PropertyHoldings,
    PropertyTitle,
    PublicContract,
    PublicContracts,
    RelatedCompanies,
    RelatedCompany,
    Website,
)
from informant.storage import load_profile_file, save_profile


def profile(name, number, **sections) -> CompanyProfile:
    return CompanyProfile(query=number, identity=Identity(name=name, company_number=number,
                                                          status="active"), **sections)


def acme() -> CompanyProfile:
    return profile(
        "ACME LIMITED", "01234567",
        ownership=Ownership(parents=[
            ParentCompany(level=1, name="ACME HOLDINGS LIMITED", company_number="07654321",
                          controls="ACME LIMITED", natures_of_control=["ownership-of-shares-75-to-100-percent"]),
            ParentCompany(level=2, name="ACME GLOBAL INC", controls="ACME HOLDINGS LIMITED",
                          country_registered="USA", is_ultimate=True)]),
        lei=LegalEntity(lei="LEI1", subsidiaries=[
            LeiEntity(lei="LEI2", name="ACME SERVICES LIMITED", country="GB", company_number="5555555")]),
        related_companies=RelatedCompanies(companies=[RelatedCompany(
            company_number="09999999", name="WIDGET CO LIMITED", shared_officers=["SMITH, Jane"])]),
        customers=[Customer(name="Widget Co", registered_name="WIDGET CO LIMITED", company_number="09999999",
                            match="exact", evidence="case study"),
                   Customer(name="Small Shop", match="none")],
        public_contracts=PublicContracts(found=2, items=[
            PublicContract(title="IT support", buyer="Leeds City Council", value=50_000),
            PublicContract(title="Framework", buyer="Leeds City Council", value=9_000_000, suppliers=40)]),
        funding=Funding(rounds=[FundingRound(year="2020", round="Series A", investors=["Big Ventures LLP"])]),
        political_donations=PoliticalDonations(searched_for=["acme"], found=2, items=[
            PoliticalDonation(recipient="Labour Party", recipient_type="Political Party", value=5000,
                              reference="C1"),
            PoliticalDonation(recipient="Jane Doe MP", recipient_type="MP - Member of Parliament",
                              to_individual=True, value=1000, reference="C2")]),
        property=PropertyHoldings(dataset="x", found=1, items=[PropertyTitle(
            title_number="T1", co_owners=[CoOwner(name="BIG BANK PLC", company_number="2065")])]),
        website=Website(analytics_ids=["G-ABC123"]),
    )


def widget() -> CompanyProfile:
    return profile("WIDGET CO LIMITED", "09999999",
                   related_companies=RelatedCompanies(companies=[RelatedCompany(
                       company_number="01234567", name="ACME LIMITED", shared_officers=["SMITH, Jane"])]),
                   website=Website(analytics_ids=["G-ABC123"]))


def edges_of(graph):
    return {(e.source, e.target, e.type) for e in graph.edges}


def test_every_kind_of_link(tmp_path):
    save_profile(acme(), out_dir=tmp_path)
    save_profile(widget(), out_dir=tmp_path)
    g = build_graph(tmp_path)
    assert g.companies_saved == 2
    got = edges_of(g)
    assert ("ch:07654321", "ch:01234567", "owns") in got  # PSC, numbered parent
    assert ("org:acme global inc", "ch:07654321", "owns") in got  # the owner's owner
    assert ("ch:01234567", "ch:05555555", "owns") in got  # LEI subsidiary (zero-padded)
    assert ("ch:01234567", "ch:09999999", "shares_officers") in got
    assert ("ch:01234567", "ch:09999999", "supplies") in got
    assert ("ch:01234567", "org:small shop", "supplies") in got
    assert ("ch:01234567", "body:leeds city council", "supplies") in got
    assert ("org:big ventures", "ch:01234567", "invests_in") in got
    assert ("ch:01234567", "party:labour party", "donated_to") in got
    assert ("ch:00002065", "ch:01234567", "co_owns_property_with") in got  # undirected: sorted ends
    assert ("ch:01234567", "ch:09999999", "shares_analytics_with") in got
    # never a node for a person: the MP who received a donation is left out
    assert not any("jane" in n.id for n in g.nodes)


def test_edges_merge_and_carry_evidence(tmp_path):
    save_profile(acme(), out_dir=tmp_path)
    save_profile(widget(), out_dir=tmp_path)
    g = build_graph(tmp_path)
    shared = [e for e in g.edges if e.type == "shares_officers"]
    assert len(shared) == 1 and not shared[0].directed  # reported by both, one edge
    assert sorted(shared[0].reported_by) == ["ch:01234567", "ch:09999999"]
    assert shared[0].evidence == ["shared officer(s): SMITH, Jane"]
    council = next(e for e in g.edges if e.target == "body:leeds city council")
    assert council.count == 2 and council.value == 50_000  # framework value not counted
    assert next(n for n in g.nodes if n.id == "ch:09999999").saved


def test_name_only_nodes_merge_into_numbered_ones(tmp_path):
    parent = profile("ACME HOLDINGS LIMITED", "07654321")
    child = profile("ACME LIMITED", "01234567", funding=Funding(rounds=[
        FundingRound(year="2021", investors=["Acme Holdings Ltd"])]))  # same legal name, no number
    save_profile(parent, out_dir=tmp_path)
    save_profile(child, out_dir=tmp_path)
    g = build_graph(tmp_path)
    assert ("ch:07654321", "ch:01234567", "invests_in") in edges_of(g)
    assert not any(n.id.startswith("org:acme holdings") for n in g.nodes)


def test_connections_between_saved_companies(tmp_path):
    save_profile(acme(), out_dir=tmp_path)
    save_profile(widget(), out_dir=tmp_path)
    third = profile("THIRD LIMITED", "03333333", public_contracts=PublicContracts(found=1, items=[
        PublicContract(title="Cleaning", buyer="Leeds City Council")]))
    save_profile(third, out_dir=tmp_path)
    g = build_graph(tmp_path)
    me = find_node(g, "3333333")
    paths = connections(g, me.id, max_hops=2)
    assert [p["company"].name for p in paths] == ["ACME LIMITED"]  # both sell to the council
    assert [step[1].id for step in paths[0]["path"]] == ["body:leeds city council", "ch:01234567"]
    assert len(neighbours(g, me.id)) == 1


def test_older_profiles_still_load(tmp_path):
    p = acme()
    data = json.loads(p.to_json())
    data["news"] = []  # how schema 0.3 stored it
    old = tmp_path / "old.json"
    old.write_text(json.dumps(data), encoding="utf-8")
    loaded = load_profile_file(old)
    assert loaded is not None and loaded.identity.name == "ACME LIMITED" and loaded.news.items == []
    assert load_profile_file(old, lenient=False) is None
    (tmp_path / "graph.json").write_text('{"nodes": []}', encoding="utf-8")
    assert load_profile_file(tmp_path / "graph.json") is None  # not a profile


def test_json_output_is_utf8_even_on_a_cp1252_pipe(monkeypatch):
    import io
    import sys

    from informant.cli import print_json

    raw = io.BytesIO()
    pipe = io.TextIOWrapper(raw, encoding="cp1252")  # what Windows gives a piped stdout
    monkeypatch.setattr(sys, "stdout", pipe)
    print_json({"headline": "Monzo → profit"})
    pipe.flush()
    assert json.loads(raw.getvalue().decode("utf-8"))["headline"] == "Monzo → profit"


def test_cli(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    save_profile(acme(), out_dir=tmp_path / "out")
    save_profile(widget(), out_dir=tmp_path / "out")
    runner = CliRunner()
    data = json.loads(runner.invoke(app, ["graph", "-o", "out", "--json"]).stdout)
    assert data["ok"] and data["companies_saved"] == 2 and data["edge_types"]["owns"] == 3
    assert (tmp_path / "out" / "graph.json").is_file()
    one = json.loads(runner.invoke(app, ["graph", "09999999", "-o", "out", "--json"]).stdout)
    assert one["node"]["name"] == "WIDGET CO LIMITED"
    assert [c["company"]["name"] for c in one["connections"]] == ["ACME LIMITED"]
    assert runner.invoke(app, ["graph", "nobody", "-o", "out", "--json"]).exit_code == 2
    text = runner.invoke(app, ["graph", "01234567", "-o", "out"]).stdout
    assert "Labour Party" in text and "shares officers" in text
    # the index still lists only profiles
    assert save_graph(tmp_path / "out")[0].companies_saved == 2
