"""Deeper Companies House data: accounts (iXBRL), capital, charges, ownership,
related companies and risk flags."""

import json
from datetime import date

import httpx
import pytest
import respx
from typer.testing import CliRunner

from informant import cli
from informant.cache import AccountsCache
from informant.schema import (
    Capital,
    CapitalEvent,
    Charge,
    Charges,
    CompanyProfile,
    Compliance,
    FinancialPeriod,
    Financials,
    Filings,
    Identity,
    Officer,
    People,
    PreviousName,
)
from informant.sources.companies_house import BASE_URL, CompaniesHouseClient, RawCompany
from informant.sources.companies_house.accounts import fetch_financials, parse_ixbrl, parse_number
from informant.sources.companies_house.capital import fetch_capital, map_charges
from informant.sources.companies_house.ownership import trace_ownership
from informant.sources.companies_house.related import fetch_related
from informant.sources.companies_house.risk import compute_risk

N = "01234567"
DOC = "https://document-api.company-information.service.gov.uk/document"
S3 = "https://s3.example/signed"

# A small iXBRL document in the shape real filings use (prefixes vary by software).
IXBRL = """<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body>
<ix:header><ix:resources>
 <xbrli:context id="cur"><xbrli:entity><xbrli:identifier>01234567</xbrli:identifier></xbrli:entity>
  <xbrli:period><xbrli:instant>2025-03-31</xbrli:instant></xbrli:period></xbrli:context>
 <xbrli:context id="prev"><xbrli:entity><xbrli:identifier>01234567</xbrli:identifier></xbrli:entity>
  <xbrli:period><xbrli:instant>2024-03-31</xbrli:instant></xbrli:period></xbrli:context>
 <xbrli:context id="cur_dur"><xbrli:entity><xbrli:identifier>01234567</xbrli:identifier></xbrli:entity>
  <xbrli:period><xbrli:startDate>2024-04-01</xbrli:startDate><xbrli:endDate>2025-03-31</xbrli:endDate></xbrli:period></xbrli:context>
 <xbrli:context id="prev_dur"><xbrli:entity><xbrli:identifier>01234567</xbrli:identifier></xbrli:entity>
  <xbrli:period><xbrli:startDate>2023-04-01</xbrli:startDate><xbrli:endDate>2024-03-31</xbrli:endDate></xbrli:period></xbrli:context>
 <xbrli:context id="cur_computers"><xbrli:entity><xbrli:identifier>01234567</xbrli:identifier>
   <xbrli:segment><xbrldi:explicitMember dimension="core:AssetClasses">core:Computers</xbrldi:explicitMember></xbrli:segment></xbrli:entity>
  <xbrli:period><xbrli:instant>2025-03-31</xbrli:instant></xbrli:period></xbrli:context>
 <xbrli:unit id="GBP"><xbrli:measure>iso4217:GBP</xbrli:measure></xbrli:unit>
 <xbrli:unit id="pure"><xbrli:measure>xbrli:pure</xbrli:measure></xbrli:unit>
</ix:resources></ix:header>
<ix:nonFraction name="frs-core:TurnoverRevenue" contextRef="cur_dur" unitRef="GBP" scale="3" format="ixt:numcommadot">1,250</ix:nonFraction>
<ix:nonFraction name="frs-core:TurnoverRevenue" contextRef="prev_dur" unitRef="GBP" scale="3" format="ixt:numcommadot">900</ix:nonFraction>
<ix:nonFraction name="frs-core:ProfitLoss" contextRef="cur_dur" unitRef="GBP" sign="-" format="ixt:numcommadot">40,000</ix:nonFraction>
<ix:nonFraction name="frs-core:FixedAssets" contextRef="cur" unitRef="GBP" format="ixt:numcommadot">12,441</ix:nonFraction>
<ix:nonFraction name="frs-core:FixedAssets" contextRef="cur_computers" unitRef="GBP" format="ixt:numcommadot">999</ix:nonFraction>
<ix:nonFraction name="frs-core:CashBankOnHand" contextRef="cur" unitRef="GBP" format="ixt:numcommadot">6,171,419</ix:nonFraction>
<ix:nonFraction name="frs-core:CashBankOnHand" contextRef="prev" unitRef="GBP" format="ixt:numdash">-</ix:nonFraction>
<ix:nonFraction name="frs-core:NetAssetsLiabilities" contextRef="cur" unitRef="GBP" format="ixt:numcommadot">6,184,992</ix:nonFraction>
<ix:nonFraction name="frs-core:NetAssetsLiabilities" contextRef="prev" unitRef="GBP" format="ixt:numcommadot">4,966,671</ix:nonFraction>
<ix:nonFraction name="frs-core:AverageNumberEmployeesDuringPeriod" contextRef="cur_dur" unitRef="pure">21</ix:nonFraction>
<ix:nonFraction name="frs-core:AverageNumberEmployeesDuringPeriod" contextRef="prev_dur" unitRef="pure">19</ix:nonFraction>
<ix:nonFraction name="frs-direp:AdvancesCreditsDirectors" contextRef="cur" unitRef="GBP">225,088</ix:nonFraction>
</body></html>"""


def test_parse_number_formats():
    assert parse_number("12,441", "ixt:numcommadot", None, None) == 12441
    assert parse_number("1.234,56", "ixt:numdotcomma", None, None) == 1234.56
    assert parse_number("1,250", "ixt:numdotdecimal", "3", None) == 1_250_000
    assert parse_number("40,000", None, None, "-") == -40000
    assert parse_number("-", "ixt:numdash", None, None) == 0
    assert parse_number("n/a", None, None, None) is None


def test_parse_ixbrl_reads_headline_figures_only():
    cur, prev = parse_ixbrl(IXBRL)
    assert cur["period_end"] == "2025-03-31" and cur["period_start"] == "2024-04-01"
    assert cur["turnover"] == 1_250_000 and cur["profit_loss"] == -40_000
    assert cur["fixed_assets"] == 12441  # not the 999 broken down by asset class
    assert cur["cash"] == 6_171_419 and cur["net_assets"] == 6_184_992
    assert cur["employees"] == 21 and cur["currency"] == "GBP"
    assert prev["period_end"] == "2024-03-31" and prev["cash"] == 0 and prev["employees"] == 19
    assert "AdvancesCreditsDirectors" not in json.dumps([cur, prev])  # directors' loans not read


def accounts_item(made_up: str, filed: str, doc_id: str) -> dict:
    return {"date": filed, "type": "AA", "category": "accounts", "transaction_id": f"t{doc_id}",
            "description": "accounts-with-accounts-type-total-exemption-full",
            "description_values": {"made_up_date": made_up},
            "links": {"document_metadata": f"{DOC}/{doc_id}"}}


class FakeRegistry:
    """Routes for one company's deeper registry data."""

    def __init__(self, number=N, accounts=(), capital=(), charges=None, filings=()):
        self.number = number
        self.by_category = {"accounts": list(accounts), "capital": list(capital)}
        self.filings = list(filings)
        respx.get(f"{BASE_URL}/company/{number}/filing-history").mock(side_effect=self._filings)
        respx.get(f"{BASE_URL}/company/{number}/charges").respond(
            json=charges if charges is not None else {"items": [], "total_count": 0})

    def _filings(self, request: httpx.Request) -> httpx.Response:
        category = request.url.params.get("category")
        items = self.by_category.get(category, []) if category else self.filings
        return httpx.Response(200, json={"items": items, "total_count": len(items)})

    def ixbrl_document(self, doc_id: str, body: str = IXBRL):
        meta = respx.get(f"{DOC}/{doc_id}").respond(json={"resources": {
            "application/pdf": {"content_length": 1}, "application/xhtml+xml": {"content_length": 2}}})
        content = respx.get(f"{DOC}/{doc_id}/content").respond(
            302, headers={"location": f"{S3}/{doc_id}"})
        stored = respx.get(f"{S3}/{doc_id}").respond(content=body.encode())
        return meta, content, stored

    def pdf_document(self, doc_id: str):
        return respx.get(f"{DOC}/{doc_id}").respond(json={"resources": {"application/pdf": {}}})


def client() -> CompaniesHouseClient:
    return CompaniesHouseClient("test-key")


@respx.mock
def test_fetch_financials_downloads_parses_and_caches(tmp_path):
    fake = FakeRegistry(accounts=[accounts_item("2025-03-31", "2025-12-29", "docA")])
    meta, content, stored = fake.ixbrl_document("docA")
    cache = AccountsCache(tmp_path)

    with client() as ch:
        fin = fetch_financials(ch, N, cache)
    assert [str(p.period_end) for p in fin.periods] == ["2025-03-31", "2024-03-31"]
    assert fin.periods[0].net_assets == 6_184_992 and str(fin.periods[0].filed_on) == "2025-12-29"
    assert "tdocA" in fin.periods[0].source_url
    # The storage redirect must be followed WITHOUT the API key.
    assert "authorization" in content.calls[0].request.headers
    assert "authorization" not in stored.calls[0].request.headers

    with client() as ch:
        again = fetch_financials(ch, N, cache)
    assert again.periods[0].net_assets == 6_184_992
    assert meta.call_count == 1 and stored.call_count == 1  # second run read the cache


@respx.mock
def test_pdf_only_accounts_are_reported_not_guessed():
    fake = FakeRegistry(accounts=[accounts_item("2025-12-27", "2026-05-19", "docP")])
    fake.pdf_document("docP")
    with client() as ch:
        fin = fetch_financials(ch, N)
    assert fin.periods == [] and "PDF only" in fin.notes[0] and "2025-12-27" in fin.notes[0]


@respx.mock
def test_newest_filing_wins_for_an_overlapping_year():
    restated = IXBRL.replace("4,966,671", "5,000,000")  # this year's filing restates last year
    fake = FakeRegistry(accounts=[accounts_item("2025-03-31", "2025-12-29", "new"),
                                  accounts_item("2024-03-31", "2024-12-31", "old")])
    fake.ixbrl_document("new", restated)
    fake.ixbrl_document("old", IXBRL.replace("2025-03-31", "2024-03-31")
                        .replace("2024-04-01", "2023-04-01"))
    with client() as ch:
        fin = fetch_financials(ch, N)
    by_end = {str(p.period_end): p for p in fin.periods}
    assert by_end["2024-03-31"].net_assets == 5_000_000


@respx.mock
def test_no_accounts_filed():
    FakeRegistry(accounts=[{"type": "AA01", "description": "change-account-reference-date",
                            "links": {"document_metadata": f"{DOC}/x"}}])
    with client() as ch:
        fin = fetch_financials(ch, N)
    assert fin.periods == [] and "no accounts" in fin.notes[0]


# --- capital and charges ------------------------------------------------------------

CAPITAL = [
    {"date": "2025-08-20", "type": "SH01", "description": "capital-allotment-shares",
     "transaction_id": "c1",
     "description_values": {"date": "2025-08-15", "capital": [{"currency": "GBP", "figure": "41,807.80"}]}},
    {"date": "2018-04-09", "type": "SH06", "description": "capital-cancellation-shares",
     "description_values": {"date": "2018-03-06", "capital": [{"currency": "GBP", "figure": "0.04"}]}},
    {"date": "2018-03-20", "type": "SH03", "description": "capital-return-purchase-own-shares",
     "description_values": None},
    {"date": "2005-01-10", "type": "169", "description": "legacy",
     "description_values": {"description": "£ ic 100/90 01/01/05 £ sr 10@1=10"}},
    {"date": "2004-01-10", "type": "88(2)R", "description": "legacy", "description_values": {}},
    {"date": "2019-01-10", "type": "SH08", "description": "capital-name-of-class-of-shares"},
]


@respx.mock
def test_capital_events_are_classified():
    FakeRegistry(capital=CAPITAL)
    with client() as ch:
        cap = fetch_capital(ch, N)
    assert [e.kind for e in cap.events] == [
        "allotment", "cancellation", "buyback", "buyback", "allotment", "other"]
    first = cap.events[0]
    assert str(first.event_date) == "2025-08-15" and str(first.filed_on) == "2025-08-20"
    assert first.share_capital == 41807.80 and first.currency == "GBP" and "c1" in first.document_url


def test_charges_mapping_and_counts():
    charges = map_charges(N, {
        "total_count": 3, "satisfied_count": 1, "part_satisfied_count": 0,
        "items": [
            {"charge_number": 3, "status": "outstanding", "created_on": "2022-03-11",
             "classification": {"description": "A registered charge"},
             "persons_entitled": [{"name": "Big Bank PLC"}],
             "particulars": {"description": "All assets"}},
            {"charge_number": 2, "status": "fully-satisfied", "created_on": "2015-01-01",
             "satisfied_on": "2019-01-01", "persons_entitled": [{"name": "Old Lender Ltd"}]},
        ]})
    assert (charges.total, charges.outstanding, charges.satisfied) == (3, 2, 1)
    assert charges.items[0].lenders == ["Big Bank PLC"] and charges.items[0].particulars == "All assets"
    assert map_charges(N, {}).total == 0


# --- ownership ------------------------------------------------------------------------


def corp(name, number, country="England And Wales", **extra):
    return {"kind": "corporate-entity-person-with-significant-control", "name": name,
            "natures_of_control": ["ownership-of-shares-75-to-100-percent"],
            "identification": {"registration_number": number, "country_registered": country,
                               "legal_form": "Private Limited Company"}, **extra}


def person(name):
    return {"kind": "individual-person-with-significant-control", "name": name,
            "natures_of_control": ["ownership-of-shares-25-to-50-percent"]}


def mock_company(number, name, pscs):
    respx.get(f"{BASE_URL}/company/{number}").respond(
        json={"company_name": name, "company_number": number, "company_status": "active"})
    respx.get(f"{BASE_URL}/company/{number}/persons-with-significant-control").respond(
        json={"items": pscs, "total_results": len(pscs)})


@respx.mock
def test_ownership_chain_up_to_foreign_parent_and_people():
    mock_company("11111111", "ACME HOLDINGS LIMITED",
                 [corp("Acme Global Inc", "5551234", "Delaware"), person("Ms Jane Founder")])
    pscs = [corp("Acme Holdings Limited", "11111111"),
            corp("Old Investor LLP", "OC399026", ceased_on="2021-02-11")]
    with client() as ch:
        own = trace_ownership(ch, N, "ACME WIDGETS LIMITED", pscs)

    assert [(p.level, p.name, p.company_number, p.is_ultimate) for p in own.parents] == [
        (1, "Acme Holdings Limited", "11111111", False),
        (2, "Acme Global Inc", None, True),  # registered abroad: the UK trail ends
    ]
    assert own.parents[0].controls == "ACME WIDGETS LIMITED" and own.parents[0].status == "active"
    assert own.parents[1].controls == "Acme Holdings Limited"
    assert [(c.name, c.of_company) for c in own.controllers] == [
        ("Ms Jane Founder", "Acme Holdings Limited")]
    assert "Old Investor LLP" not in [p.name for p in own.parents]  # ceased


@respx.mock
def test_ownership_guards_against_loops_and_number_collisions():
    # A circular pair, and a foreign number that happens to match an unrelated UK company.
    mock_company("11111111", "LOOP ONE LIMITED", [corp("Loop Two Limited", "22222222")])
    mock_company("22222222", "LOOP TWO LIMITED", [corp("Loop One Limited", "11111111")])
    mock_company("33333333", "TOTALLY DIFFERENT TRADING LIMITED", [])
    with client() as ch:
        own = trace_ownership(ch, N, "CHILD LIMITED", [
            corp("Loop One Limited", "11111111"), corp("Zeta GmbH", "33333333", "Germany")])
    names = [p.name for p in own.parents]
    assert names.count("Loop One Limited") == 2 and names.count("Loop Two Limited") == 1
    zeta = next(p for p in own.parents if p.name == "Zeta GmbH")
    assert zeta.company_number is None and zeta.is_ultimate

    with client() as ch:
        none = trace_ownership(ch, N, "WIDELY HELD PLC", [])
    assert none.parents == [] and "no person with significant control" in none.notes[0]


# --- related companies ------------------------------------------------------------------


def officer(name, role="director", oid=None, resigned=None):
    oid = oid or name.split(",")[0].lower()
    return {"name": name, "officer_role": role, "resigned_on": resigned,
            "links": {"officer": {"appointments": f"/officers/{oid}/appointments"}}}


def appointments(oid, companies, total=None):
    items = [{"appointed_to": {"company_number": n, "company_name": name, "company_status": status},
              "officer_role": "director", "resigned_on": resigned}
             for n, name, status, resigned in companies]
    return respx.get(f"{BASE_URL}/officers/{oid}/appointments").respond(
        json={"items": items, "total_results": total if total is not None else len(items)})


@respx.mock
def test_related_companies_from_shared_officers():
    appointments("smith", [(N, "ACME WIDGETS LIMITED", "active", None),
                           ("22222222", "ACME PROPERTY LIMITED", "active", None),
                           ("33333333", "OLD VENTURE LTD", "dissolved", "2019-01-01")])
    appointments("jones", [("22222222", "ACME PROPERTY LIMITED", "active", None),
                           ("44444444", "JONES CONSULTING LTD", "active", None)])
    nominee = appointments("nominee", [(f"9{i:07d}", f"CLIENT {i} LTD", "active", None)
                                       for i in range(40)])
    gone = appointments("gone", [("55555555", "SHOULD NOT APPEAR LTD", "active", None)])
    officers = [officer("SMITH, Jane"), officer("JONES, Bob"),
                officer("NOMINEE SECRETARIES LTD", "corporate-secretary", "nominee"),
                officer("GONE, Al", oid="gone", resigned="2020-01-01")]
    with client() as ch:
        rel = fetch_related(ch, N, officers)

    assert [(c.name, c.shared_officers) for c in rel.companies] == [
        ("ACME PROPERTY LIMITED", ["SMITH, Jane", "JONES, Bob"]),  # most shared first
        ("JONES CONSULTING LTD", ["JONES, Bob"]),
    ]  # the company itself, resigned appointments and resigned officers are left out
    assert nominee.call_count == 1 and gone.call_count == 0
    assert any("NOMINEE SECRETARIES LTD holds 40 appointments" in n for n in rel.notes)


# --- risk flags ------------------------------------------------------------------------------

TODAY = date(2026, 11, 12)


def make_profile(**identity) -> CompanyProfile:
    ident = dict(name="ACME WIDGETS LIMITED", company_number=N, status="active",
                 company_type="ltd", incorporated_on="2010-03-15")
    ident.update(identity)
    return CompanyProfile(query=N, identity=Identity(**ident))


def codes(profile, raw=None):
    return {f.code: f for f in compute_risk(profile, raw, today=TODAY).flags}


def test_clean_company_has_no_flags():
    p = make_profile()
    p.people = People(officers=[Officer(name="A", role="director"), Officer(name="B", role="director")])
    assert compute_risk(p, RawCompany(profile={}), today=TODAY).flags == []


def test_formal_status_flags_sorted_by_severity():
    p = make_profile(status="liquidation", has_insolvency_history=True)
    p.filings = Filings(compliance=Compliance(accounts_overdue=True, accounts_next_due="2026-03-31",
                                              confirmation_statement_overdue=True))
    p.people = People(officers=[Officer(name="A", role="director")])
    flags = compute_risk(p, today=TODAY).flags
    assert [f.severity for f in flags] == sorted([f.severity for f in flags],
                                                 key=["high", "warning", "info"].index)
    found = {f.code for f in flags}
    assert {"status_distress", "insolvency_history", "accounts_overdue",
            "confirmation_statement_overdue", "sole_director"} <= found


def test_strike_off_and_address_and_late_filing_from_filing_history():
    filings = [
        {"date": "2026-09-01", "type": "GAZ1", "category": "gazette"},
        {"date": "2026-05-01", "type": "AD01", "category": "address"},
        {"date": "2025-10-01", "type": "AD01", "category": "address"},
        {"date": "2026-03-20", "type": "AA", "description": "accounts-with-accounts-type-micro-entity",
         "description_values": {"made_up_date": "2025-03-31"}},  # due 2025-12-31: ~80 days late
        {"date": "2025-01-03", "type": "AA", "description": "accounts-with-accounts-type-micro-entity",
         "description_values": {"made_up_date": "2024-03-31"}},  # 3 days over: within tolerance
    ]
    found = codes(make_profile(), RawCompany(profile={}, filings=filings))
    assert found["strike_off_notice"].severity == "high"
    assert "2 times" in found["address_changes"].message
    assert "2025-03-31" in found["accounts_filed_late"].evidence
    assert "2024-03-31" not in found["accounts_filed_late"].evidence

    discontinued = [{"date": "2026-10-01", "type": "DISS40", "category": "gazette"}] + filings
    assert "strike_off_discontinued" in codes(make_profile(), RawCompany(profile={}, filings=discontinued))


def test_plc_deadline_and_first_accounts_are_not_flagged():
    plc = [{"date": "2025-06-30", "type": "AA", "description": "accounts-with-accounts-type-group",
            "description_values": {"made_up_date": "2024-12-28"}}]  # year end 31 Dec, due 30 June
    assert "accounts_filed_late" not in codes(make_profile(company_type="plc"),
                                              RawCompany(profile={}, filings=plc))
    first = [{"date": "2026-10-01", "type": "AA", "description": "accounts-with-accounts-type-micro-entity",
              "description_values": {"made_up_date": "2025-06-30"}}]
    assert "accounts_filed_late" not in codes(make_profile(incorporated_on="2024-06-15"),
                                              RawCompany(profile={}, filings=first))


def test_people_capital_borrowing_and_finance_flags():
    p = make_profile(previous_names=[PreviousName(name="OLD NAME LTD", ceased_on="2025-12-01")])
    p.people = People(officers=(
        [Officer(name=f"Current {i}", role="director") for i in range(4)]
        + [Officer(name=f"Left {i}", role="director", resigned_on="2026-07-20") for i in range(3)]
        + [Officer(name="Long gone", role="director", resigned_on="2015-01-01")]))
    p.capital = Capital(events=[CapitalEvent(kind="allotment", event_date="2025-08-15"),
                                CapitalEvent(kind="allotment", event_date="2012-01-01")])
    p.charges = Charges(total=2, outstanding=1, satisfied=1, items=[
        Charge(status="outstanding", lenders=["Big Bank PLC"]),
        Charge(status="fully-satisfied", lenders=["Old Lender Ltd"])])
    p.financials = Financials(periods=[
        FinancialPeriod(period_end="2025-03-31", net_assets=-50_000, cash=20_000, employees=10),
        FinancialPeriod(period_end="2024-03-31", net_assets=400_000, cash=300_000, employees=20)])
    found = codes(p)

    assert "3 directors resigned" in found["director_turnover"].message
    assert found["recent_name_change"].evidence.startswith("previously OLD NAME LTD")
    assert found["recent_share_allotments"].message.startswith("1 share allotment")
    assert found["outstanding_charges"].evidence == "lenders: Big Bank PLC"
    assert found["negative_net_assets"].severity == "warning"
    assert "net_assets_fall" in found and "93%" in found["cash_fall"].message
    assert found["headcount_fall"].evidence == "20 to 10"

    p.financials.periods[0].employees = 30
    assert codes(p)["headcount_growth"].evidence == "20 to 30"


# --- the whole thing through the CLI -----------------------------------------------------------

PROFILE = {
    "company_name": "ACME WIDGETS LIMITED", "company_number": N, "company_status": "active",
    "type": "ltd", "date_of_creation": "2010-03-15", "sic_codes": ["62012"], "has_charges": False,
    "accounts": {"next_due": "2026-12-31", "overdue": False,
                 "last_accounts": {"made_up_to": "2025-03-31", "type": "total-exemption-full"}},
    "confirmation_statement": {"next_due": "2027-03-29", "overdue": False},
}


@pytest.fixture
def full_registry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "k")
    monkeypatch.setenv("INFORMANT_REGISTRY", "full")
    return tmp_path


def mock_core(charges=None):
    respx.get(f"{BASE_URL}/company/{N}").respond(json=PROFILE)
    respx.get(f"{BASE_URL}/company/{N}/officers").respond(json={"total_results": 2, "items": [
        dict(officer("SMITH, Jane"), appointed_on="2010-03-15"),
        dict(officer("JONES, Bob"), appointed_on="2012-01-01")]})
    respx.get(f"{BASE_URL}/company/{N}/persons-with-significant-control").respond(
        json={"items": [corp("Acme Holdings Limited", "11111111")], "total_results": 1})
    mock_company("11111111", "ACME HOLDINGS LIMITED", [person("Ms Jane Founder")])
    appointments("smith", [(N, "ACME WIDGETS LIMITED", "active", None),
                           ("22222222", "ACME PROPERTY LIMITED", "active", None)])
    appointments("jones", [(N, "ACME WIDGETS LIMITED", "active", None)])
    fake = FakeRegistry(accounts=[accounts_item("2025-03-31", "2025-12-29", "docA")],
                        capital=CAPITAL[:1], charges=charges, filings=[])
    fake.ixbrl_document("docA")
    return fake


@respx.mock
def test_full_lookup_fills_every_registry_section(full_registry):
    mock_core(charges={"total_count": 1, "satisfied_count": 0, "items": [
        {"charge_number": 1, "status": "outstanding", "persons_entitled": [{"name": "Big Bank PLC"}]}]})
    result = CliRunner().invoke(cli.app, ["lookup", N, "--no-web", "--json", "--progress"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    p = data["profile"]

    assert p["schema_version"] == "0.5.0"
    assert p["financials"]["periods"][0]["net_assets"] == 6_184_992
    assert p["capital"]["events"][0]["kind"] == "allotment"
    assert p["charges"]["outstanding"] == 1 and p["identity"]["has_charges"] is True
    assert p["ownership"]["parents"][0]["name"] == "Acme Holdings Limited"
    assert p["ownership"]["parents"][0]["is_ultimate"] is True
    assert p["ownership"]["controllers"][0]["of_company"] == "Acme Holdings Limited"
    assert [c["name"] for c in p["related_companies"]["companies"]] == ["ACME PROPERTY LIMITED"]
    flag_codes = {f["code"] for f in p["risk"]["flags"]}
    assert {"outstanding_charges", "recent_share_allotments"} <= flag_codes

    index = json.loads((full_registry / "output" / "index.json").read_text())
    entry = index["companies"][0]
    assert entry["net_assets"] == 6_184_992 and entry["employees"] == 21
    assert entry["ultimate_parent"] == "Acme Holdings Limited" and entry["flags"]["info"] >= 2

    events = [json.loads(l) for l in result.stderr.splitlines() if l.strip()]
    stage = [(e["status"], e["message"]) for e in events if e["stage"] == "registry_details"]
    assert stage[0][0] == "started" and stage[-1][0] == "done"
    assert ("progress", "Tracing ownership") in stage


@respx.mock
def test_one_failing_step_does_not_stop_the_rest(full_registry):
    mock_core()
    respx.get(f"{BASE_URL}/company/{N}/charges").respond(500, text="boom")
    result = CliRunner().invoke(cli.app, ["lookup", N, "--no-web", "--json"])
    assert result.exit_code == 0, result.output
    p = json.loads(result.stdout)["profile"]
    assert "could not be read" in p["charges"]["notes"][0]
    assert p["financials"]["periods"] and p["ownership"]["parents"]  # other steps unaffected


@respx.mock
def test_basic_depth_skips_extra_calls_and_bad_value_is_rejected(full_registry):
    fake = mock_core()
    result = CliRunner().invoke(cli.app, ["lookup", N, "--no-web", "--json", "--registry", "basic"])
    p = json.loads(result.stdout)["profile"]
    assert p["financials"]["periods"] == [] and p["ownership"]["parents"] == []
    assert not any("charges" in str(c.request.url) for c in respx.calls)

    bad = CliRunner().invoke(cli.app, ["lookup", N, "--json", "--registry", "everything"])
    assert bad.exit_code == 3
