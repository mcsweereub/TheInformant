import httpx
import pytest
import respx

from informant.companies_house import (
    BASE_URL,
    CompaniesHouseClient,
    CompaniesHouseError,
    fetch_company,
    normalise_company_number,
)
from informant.schema import CompanyProfile

PROFILE = {
    "company_name": "EXAMPLE TRADING LIMITED",
    "company_number": "01234567",
    "company_status": "active",
    "type": "ltd",
    "jurisdiction": "england-wales",
    "date_of_creation": "2010-03-15",
    "sic_codes": ["62012", "62020"],
    "registered_office_address": {
        "address_line_1": "1 High Street",
        "locality": "London",
        "postal_code": "EC1A 1AA",
        "country": "United Kingdom",
    },
    "previous_company_names": [
        {"name": "EXAMPLE LTD", "effective_from": "2010-03-15", "ceased_on": "2015-01-01"}
    ],
    "accounts": {
        "next_due": "2027-03-31",
        "overdue": False,
        "last_accounts": {"made_up_to": "2025-06-30", "type": "small"},
    },
    "confirmation_statement": {"next_due": "2027-03-29", "overdue": False},
    "has_charges": True,
    "has_insolvency_history": False,
}

OFFICERS = {
    "total_results": 2,
    "items": [
        {"name": "SMITH, Jane", "officer_role": "director", "appointed_on": "2010-03-15",
         "occupation": "Engineer", "nationality": "British",
         "date_of_birth": {"month": 1, "year": 1980},
         "address": {"address_line_1": "should not be stored"}},
        {"name": "JONES, Bob", "officer_role": "secretary", "appointed_on": "2010-03-15",
         "resigned_on": "2020-01-01"},
    ],
}

PSCS = {
    "total_results": 1,
    "items": [
        {"name": "Ms Jane Smith", "kind": "individual-person-with-significant-control",
         "natures_of_control": ["ownership-of-shares-75-to-100-percent"],
         "notified_on": "2016-04-06"}
    ],
}

FILINGS = {
    "total_count": 1,
    "items": [
        {"date": "2025-09-01", "category": "accounts", "type": "AA",
         "description": "accounts-with-accounts-type-small",
         "description_values": {"made_up_date": "2025-06-30"},
         "transaction_id": "MzQ1Njc4"}
    ],
}


@pytest.mark.parametrize(
    "raw,expected",
    [("01234567", "01234567"), ("1234567", "01234567"), ("sc123456", "SC123456"),
     (" OC 301234 ", "OC301234"), ("Tesco", None), ("123456789", None)],
)
def test_normalise_company_number(raw, expected):
    assert normalise_company_number(raw) == expected


def test_missing_key_raises():
    with pytest.raises(CompaniesHouseError):
        CompaniesHouseClient("")


@respx.mock
def test_fetch_company_maps_to_schema():
    respx.get(f"{BASE_URL}/company/01234567").respond(json=PROFILE)
    respx.get(f"{BASE_URL}/company/01234567/officers").respond(json=OFFICERS)
    respx.get(f"{BASE_URL}/company/01234567/persons-with-significant-control").respond(json=PSCS)
    respx.get(f"{BASE_URL}/company/01234567/filing-history").respond(json=FILINGS)

    with CompaniesHouseClient("test-key") as ch:
        data = fetch_company(ch, "01234567")

    assert data.identity.name == "EXAMPLE TRADING LIMITED"
    assert str(data.identity.incorporated_on) == "2010-03-15"
    assert [s.code for s in data.classification.sic_codes] == ["62012", "62020"]
    assert data.locations[0].kind == "registered_office"
    assert data.locations[0].address_lines == ["1 High Street"]
    assert len(data.people.officers) == 2
    assert [o.active for o in data.people.officers] == [True, False]
    assert data.people.significant_control[0].natures_of_control
    assert data.filings.compliance.last_accounts_type == "small"
    assert data.filings.recent[0].description.endswith("(2025-06-30)")
    assert data.filings.recent[0].document_url

    profile = CompanyProfile(
        query="01234567",
        identity=data.identity,
        classification=data.classification,
        locations=data.locations,
        people=data.people,
        filings=data.filings,
    )
    out = profile.to_json()
    # Personal data minimisation: officer addresses / DOBs never stored.
    assert "should not be stored" not in out
    assert "date_of_birth" not in out
    # Round trip: the JSON parses back into the schema.
    assert CompanyProfile.model_validate_json(out).identity.company_number == "01234567"


@respx.mock
def test_missing_psc_register_is_empty_not_error():
    respx.get(f"{BASE_URL}/company/01234567").respond(json=PROFILE)
    respx.get(f"{BASE_URL}/company/01234567/officers").respond(json=OFFICERS)
    respx.get(f"{BASE_URL}/company/01234567/persons-with-significant-control").respond(404)
    respx.get(f"{BASE_URL}/company/01234567/filing-history").respond(json=FILINGS)

    with CompaniesHouseClient("test-key") as ch:
        data = fetch_company(ch, "01234567")
    assert data.people.significant_control == []


@respx.mock
def test_retries_on_rate_limit():
    route = respx.get(f"{BASE_URL}/company/01234567")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json=PROFILE),
    ]
    with CompaniesHouseClient("test-key") as ch:
        assert ch.profile("01234567")["company_name"] == "EXAMPLE TRADING LIMITED"
    assert route.call_count == 2


@respx.mock
def test_bad_key_gives_clear_error():
    respx.get(f"{BASE_URL}/company/01234567").respond(401)
    with CompaniesHouseClient("bad") as ch, pytest.raises(CompaniesHouseError, match="API key"):
        ch.profile("01234567")
