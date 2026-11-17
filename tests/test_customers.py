"""Customers: extraction checks, matching on Companies House, index links, and
researching them later with `informant customers`."""

import json

import pytest
import respx
from typer.testing import CliRunner

from informant import cli
from informant.llm.prompts import ExtractedCustomer
from informant.llm.result import finish
from informant.pipeline import LookupResult, customers_to_research
from informant.schema import CompanyProfile, Customer, Identity
from informant.sources.companies_house import BASE_URL, CompaniesHouseClient
from informant.sources.companies_house.customers import match_customer, resolve_customers
from informant.storage import link_customers, load_index, save_profile
from test_checks import extraction


def cust(name, evidence="case study"):
    return ExtractedCustomer(name=name, evidence=evidence, source_page="https://x/")


TEXT = ("Case studies: BP, Centrica and Gazprom Marketing & Trading use EOS. "
        "Trusted by Gunvor. Mr John Smith said it was great. Planlogic serves leading banks.")


def test_customer_checks():
    data = extraction(customers=[
        cust("BP"), cust("Centrica"), cust("Centrica plc", "client list"),  # duplicate
        cust("Gazprom Marketing & Trading"), cust("Gunvor", "trusted by"),
        cust("leading banks"), cust("Mr John Smith"),  # generic phrase, a person
        cust("Planlogic Software"),  # the company itself
        cust("Shell"),  # not in the text
    ])
    notes: list[str] = []
    d = finish(data, "m", notes, source_text=TEXT, company_name="PLANLOGIC SOFTWARE LIMITED").data
    assert [c.name for c in d.customers] == ["BP", "Centrica", "Gazprom Marketing & Trading", "Gunvor"]
    assert any("Shell" in n for n in notes)


# --- matching on the register -------------------------------------------------------------


def search_results(items):
    return {"items": [{"company_number": n, "title": t, "company_status": s} for n, t, s in items]}


@respx.mock
def test_match_customer_exact_likely_and_none():
    def search(request):
        q = request.url.params["q"]
        data = {
            "Centrica": [("03033654", "CENTRICA PLC", "active"),
                         ("05555555", "CENTRICA ENERGY LIMITED", "active")],
            "BP": [("00102498", "B.P. P.L.C.", "dissolved"), ("01234567", "BP P.L.C.", "active")],
            "Octopus Energy": [("09263424", "OCTOPUS ENERGY LIMITED", "active")],
            "Acme": [("11111111", "ACME WIDGETS LIMITED", "active")],
            "Widget": [("22222222", "WIDGET ONE LTD", "active"), ("33333333", "WIDGET TWO LTD", "active")],
            "Gunvor": [],
        }[q]
        return respx.MockResponse(200, json=search_results(data))
    respx.get(f"{BASE_URL}/search/companies").mock(side_effect=search)

    with CompaniesHouseClient("k") as ch:
        customers = {n: Customer(name=n) for n in ("Centrica", "BP", "Octopus Energy", "Acme", "Widget", "Gunvor")}
        for c in customers.values():
            match_customer(ch, c)
    assert (customers["Centrica"].company_number, customers["Centrica"].match) == ("03033654", "exact")
    assert customers["BP"].company_number == "01234567"  # the active one of two exact matches
    assert customers["Octopus Energy"].match == "exact"
    assert (customers["Acme"].company_number, customers["Acme"].match) == ("11111111", "likely")
    assert customers["Widget"].match == "none"  # two candidates: don't guess
    assert customers["Gunvor"].match == "none" and customers["Gunvor"].company_number is None


@respx.mock
def test_resolve_drops_the_company_itself():
    respx.get(f"{BASE_URL}/search/companies").respond(
        json=search_results([("09461711", "PLANLOGIC SOFTWARE LIMITED", "active")]))
    customers = [Customer(name="Planlogic Software")]
    with CompaniesHouseClient("k") as ch:
        resolve_customers(ch, customers, own_number="09461711")
    assert customers == []


def test_customers_to_research_prefers_exact_active_matches():
    profile = CompanyProfile(query="x", identity=Identity(name="X"), customers=[
        Customer(name="Likely Co", company_number="1", match="likely", status="active"),
        Customer(name="Dissolved Co", company_number="2", match="exact", status="dissolved"),
        Customer(name="Exact Co", company_number="3", match="exact", status="active"),
        Customer(name="Foreign Co", match="none"),
    ])
    assert [c.name for c in customers_to_research(profile, 5)] == ["Exact Co", "Likely Co"]
    assert [c.name for c in customers_to_research(profile, 1)] == ["Exact Co"]


# --- index links -------------------------------------------------------------------------------


def test_index_links_customers_both_ways(tmp_path):
    supplier = CompanyProfile(query="x", identity=Identity(name="PLANLOGIC SOFTWARE LIMITED",
                                                           company_number="09461711"),
                              customers=[Customer(name="Centrica", company_number="03033654"),
                                         Customer(name="Gunvor")])
    customer = CompanyProfile(query="y", identity=Identity(name="CENTRICA PLC", company_number="03033654"))
    save_profile(customer, out_dir=tmp_path)  # order doesn't matter
    save_profile(supplier, out_dir=tmp_path)
    by_number = {c["company_number"]: c for c in load_index(tmp_path)["companies"]}
    assert by_number["09461711"]["customers"] == [
        {"name": "Centrica", "company_number": "03033654"}, {"name": "Gunvor", "company_number": None}]
    assert by_number["03033654"]["customer_of"] == [
        {"name": "PLANLOGIC SOFTWARE LIMITED", "company_number": "09461711"}]
    assert by_number["09461711"]["customer_of"] == []


def test_link_customers_ignores_unmatched():
    entries = [{"company_number": "1", "name": "A", "customers": [{"name": "Z", "company_number": None}]}]
    link_customers(entries)
    assert entries[0]["customer_of"] == []


# --- researching customers later ------------------------------------------------------------------


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    profile = CompanyProfile(query="x", identity=Identity(name="PLANLOGIC SOFTWARE LIMITED",
                                                          company_number="09461711"),
                             customers=[
                                 Customer(name="Centrica", company_number="03033654", match="exact",
                                          status="active", registered_name="CENTRICA PLC"),
                                 Customer(name="BP", company_number="01234567", match="exact",
                                          status="active", registered_name="BP P.L.C."),
                                 Customer(name="Gunvor", match="none")])
    save_profile(profile, out_dir=tmp_path / "output")
    return tmp_path


def test_customers_command_lists_without_researching(saved, monkeypatch):
    called = []
    monkeypatch.setattr("informant.pipeline.research_company", lambda *a, **k: called.append(a))
    result = CliRunner().invoke(cli.app, ["customers", "09461711", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert [c["name"] for c in data["customers"]] == ["Centrica", "BP", "Gunvor"]
    assert data["customer_research"] == [] and called == []


def test_customers_command_researches_and_saves(saved, monkeypatch):
    seen = []

    def fake_research(number, progress=None, **options):
        seen.append((number, options))
        if number == "01234567":
            from informant.errors import UpstreamError
            raise UpstreamError("Companies House is down")
        return LookupResult(profile=CompanyProfile(query=number, identity=Identity(
            name="CENTRICA PLC", company_number=number)), matched_by="number")

    monkeypatch.setattr("informant.pipeline.research_company", fake_research)
    result = CliRunner().invoke(cli.app, ["customers", "09461711", "--research", "--no-llm", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)

    assert [n for n, _ in seen] == ["03033654", "01234567"]  # Gunvor isn't on the register
    assert seen[0][1]["llm"] is False and "website" not in seen[0][1]
    runs = {r["company_number"]: r for r in data["customer_research"]}
    assert runs["03033654"]["ok"] and runs["03033654"]["path"].endswith(".json")
    assert not runs["01234567"]["ok"] and "down" in runs["01234567"]["error"]

    index = {c["company_number"]: c for c in load_index(saved / "output")["companies"]}
    assert index["03033654"]["customer_of"][0]["company_number"] == "09461711"
    main = {c["name"]: c for c in data["customers"]}
    assert main["Centrica"]["researched"] and not main["BP"]["researched"]


def test_customers_command_needs_a_saved_profile(saved):
    result = CliRunner().invoke(cli.app, ["customers", "00000001", "--json"])
    assert result.exit_code == 2 and json.loads(result.stdout)["error"]["code"] == "not_found"


def test_descriptions_are_not_customer_names():
    text = "Financial advisers throughout Australia use EOS. So does the Bank of England."
    data = extraction(customers=[cust("Financial advisers throughout Australia"), cust("Bank of England")])
    assert [c.name for c in finish(data, "m", [], source_text=text).data.customers] == ["Bank of England"]
