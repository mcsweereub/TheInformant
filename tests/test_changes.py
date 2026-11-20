"""Change tracking: comparing two lookups, the history folder, and `informant changes`."""

import json
import time
from datetime import date, datetime, timedelta, timezone

import respx

from informant.changes import compare
from informant.schema import (
    Charge,
    Charges,
    CompanyProfile,
    Domain,
    Filing,
    Filings,
    FinancialPeriod,
    Financials,
    Hiring,
    Identity,
    Officer,
    People,
    PoliticalDonation,
    PoliticalDonations,
    Risk,
    RiskFlag,
    Source,
    Website,
)
from informant.storage import (
    MAX_SNAPSHOTS,
    compare_snapshots,
    list_snapshots,
    load_index,
    save_profile,
)
from test_cli_json import NUMBER, invoke, mock_companies_house, parse, workdir  # noqa: F401

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
SRC = [Source(name="companies_house")]


def profile(when=T0, **sections) -> CompanyProfile:
    base = dict(query="x", generated_at=when,
                identity=Identity(name="ACME LTD", company_number="01234567", status="active"))
    base.update(sections)
    return CompanyProfile(**base)


def summaries(comparison) -> list[str]:
    return [f"{c.section}|{c.kind}|{c.summary}" for c in comparison.changes]


def test_identical_lookups_have_no_changes():
    p = profile(people=People(officers=[Officer(name="SMITH, Jane", role="director")]))
    later = p.model_copy(update={"generated_at": T0 + timedelta(days=30)})
    assert compare(p, later).changes == []


def test_registry_changes():
    old = profile(
        people=People(officers=[Officer(name="SMITH, Jane", role="director"),
                                Officer(name="JONES, Bob", role="director")]),
        filings=Filings(recent=[Filing(date=date(2026, 9, 1), type="CS01", description="confirmation-statement")]),
        financials=Financials(sources=SRC, periods=[FinancialPeriod(period_end=date(2025, 3, 31), net_assets=100)]),
        charges=Charges(sources=SRC, items=[Charge(charge_number=1, created_on=date(2020, 1, 1),
                                                   status="outstanding", classification="debenture")]),
        risk=Risk(flags=[RiskFlag(code="accounts_overdue", severity="high", message="Accounts overdue")]))
    new = profile(
        when=T0 + timedelta(days=60),
        identity=Identity(name="ACME LTD", company_number="01234567", status="liquidation"),
        people=People(officers=[Officer(name="SMITH, Jane", role="director"),
                                Officer(name="JONES, Bob", role="director", resigned_on=date(2026, 10, 20)),
                                Officer(name="BROWN, Ann", role="director", appointed_on=date(2026, 10, 21))]),
        filings=Filings(recent=[Filing(date=date(2026, 11, 1), type="AA", description="accounts-with-accounts-type-small"),
                                Filing(date=date(2026, 9, 1), type="CS01", description="confirmation-statement")]),
        financials=Financials(sources=SRC, periods=[
            FinancialPeriod(period_end=date(2026, 3, 31), net_assets=250, employees=12),
            FinancialPeriod(period_end=date(2025, 3, 31), net_assets=100)]),
        charges=Charges(sources=SRC, items=[Charge(charge_number=1, created_on=date(2020, 1, 1),
                                                   status="fully-satisfied", classification="debenture")]),
        risk=Risk(flags=[RiskFlag(code="insolvency", severity="high", message="In liquidation")]))
    got = summaries(compare(old, new))
    assert "identity|changed|status: active -> liquidation" in got
    assert "officers|added|appointed: BROWN, Ann (director, 2026-10-21)" in got
    assert "officers|removed|resigned: JONES, Bob (director, 2026-10-20)" in got
    assert "filings|added|new filing: 2026-11-01 accounts-with-accounts-type-small" in got
    assert any(s.startswith("financials|added|accounts for 2026-03-31") for s in got)
    assert "charges|changed|charge 2020-01-01 debenture: outstanding -> fully-satisfied" in got
    assert "risk|added|new flag: [high] In liquidation" in got
    assert "risk|removed|flag cleared: [high] Accounts overdue" in got


def test_other_sections():
    old = profile(
        political_donations=PoliticalDonations(searched_for=["acme"]),
        hiring=Hiring(provider="greenhouse", total=40, departments={"Engineering": 20, "Sales": 5}),
        domain=Domain(name="acme.com", email_provider="Google Workspace", subdomains=["www.acme.com"]),
        website=Website(url="https://acme.com", llm_model="m"), tech_stack=["WordPress"])
    new = profile(
        when=T0 + timedelta(days=7),
        political_donations=PoliticalDonations(searched_for=["acme"], items=[PoliticalDonation(
            recipient="Labour Party", recipient_type="Political Party", value=5000, reference="C1",
            accepted_on=date(2026, 10, 3))]),
        hiring=Hiring(provider="greenhouse", total=12, departments={"Engineering": 4, "Sales": 5}),
        domain=Domain(name="acme.com", email_provider="Microsoft 365",
                      subdomains=["www.acme.com", "portal.bigclient.acme.com"]),
        website=Website(url="https://acme.com", llm_model="m"), tech_stack=["WordPress", "HubSpot"])
    got = summaries(compare(old, new))
    assert "political_donations|added|new donation: 2026-10-03 £5k to Labour Party" in got
    assert "hiring|changed|open roles: 40 -> 12" in got
    assert "hiring|changed|Engineering roles: 20 -> 4" in got  # Sales unchanged: not listed
    assert "domain|changed|email provider: Google Workspace -> Microsoft 365" in got
    assert "domain|added|new subdomain: portal.bigclient.acme.com" in got
    assert "website|added|new technology: HubSpot" in got


def test_sections_one_lookup_skipped_are_not_compared():
    full = profile(website=Website(url="https://acme.com", llm_model="m"), tech_stack=["WordPress"],
                   domain=Domain(name="acme.com", email_provider="Microsoft 365"))
    no_web = profile(when=T0 + timedelta(days=1))  # --no-web, domain skipped
    comparison = compare(full, no_web)
    assert comparison.changes == []  # nothing reported as removed
    assert set(comparison.not_compared) == {"website", "products", "domain"}


# --- the history folder ---------------------------------------------------------------------------


def test_each_save_keeps_a_dated_copy_and_the_index_counts_changes(tmp_path):
    save_profile(profile(), out_dir=tmp_path)
    assert len(list_snapshots(tmp_path, "01234567")) == 1
    later = profile(when=T0 + timedelta(days=5),
                    identity=Identity(name="ACME LTD", company_number="01234567", status="dissolved"))
    save_profile(later, out_dir=tmp_path)
    snaps = list_snapshots(tmp_path, "01234567")
    assert [s.name for s in snaps] == ["20261001T090000Z.json", "20261006T090000Z.json"]
    entry = load_index(tmp_path)["companies"][0]
    assert entry["lookups"] == 2 and entry["changes_since_previous"] == 1
    assert entry["previous_lookup_at"].startswith("2026-10-01")
    assert not list(tmp_path.glob("history/*/*.tmp"))


def test_existing_profile_becomes_the_starting_point(tmp_path, monkeypatch):
    monkeypatch.setenv("INFORMANT_HISTORY", "off")  # saved before change tracking existed
    save_profile(profile(), out_dir=tmp_path)
    assert list_snapshots(tmp_path, "01234567") == []
    monkeypatch.setenv("INFORMANT_HISTORY", "on")
    save_profile(profile(when=T0 + timedelta(days=1),
                         identity=Identity(name="ACME LTD", company_number="01234567", status="dissolved")),
                 out_dir=tmp_path)
    assert len(list_snapshots(tmp_path, "01234567")) == 2
    assert summaries(compare_snapshots(tmp_path, "01234567")) == [
        "identity|changed|status: active -> dissolved"]


def test_since_and_pruning(tmp_path, monkeypatch):
    monkeypatch.setattr("informant.storage.MAX_SNAPSHOTS", 3)
    statuses = ["active", "liquidation", "administration", "dissolved"]
    for i, status in enumerate(statuses):
        save_profile(profile(when=T0 + timedelta(days=10 * i),
                             identity=Identity(name="ACME LTD", company_number="01234567", status=status)),
                     out_dir=tmp_path)
    assert len(list_snapshots(tmp_path, "01234567")) == 3  # the oldest was pruned
    since = datetime(2026, 10, 12, tzinfo=timezone.utc)  # the lookup of 11 October
    assert summaries(compare_snapshots(tmp_path, "01234567", since=since)) == [
        "identity|changed|status: liquidation -> dissolved"]
    assert MAX_SNAPSHOTS >= 10  # the real limit


# --- the CLI -------------------------------------------------------------------------------------


@respx.mock
def test_changes_command_and_lookup_json(workdir):  # noqa: F811
    mock_companies_house()
    first = parse(invoke("lookup", NUMBER, "--no-web", "--json"))
    assert first["changes"] is None  # nothing to compare with yet
    result = invoke("changes", NUMBER, "--json")
    assert result.exit_code == 2 and json.loads(result.stdout)["error"]["code"] == "not_found"

    time.sleep(1.1)  # copies are named by the second; a re-save of one lookup overwrites its copy
    second = parse(invoke("lookup", NUMBER, "--no-web", "--json"))
    assert second["changes"]["changes"] == []  # same mocked data both times
    data = parse(invoke("changes", NUMBER, "--json"))
    assert data["ok"] and data["company_number"] == NUMBER and data["changes"] == []
    assert invoke("changes", NUMBER, "--since", "not-a-date", "--json").exit_code == 3
