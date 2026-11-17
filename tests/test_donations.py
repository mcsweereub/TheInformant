"""Political donations from the Electoral Commission's register (HTTP mocked)."""

import httpx
import respx

from informant.sources.donations import SEARCH_URL, ec_date, fetch_donations, query_for

# 2026-06-18 00:00 in UK summer time is 2026-06-17 23:00 UTC.
JUNE_18 = "/Date(1781737200000)/"


def row(id, donor="J C Bamford Excavators Ltd", number="561597", status="Company",
        entity_type="Political Party", recipient="Conservative and Unionist Party", donee_type=None,
        value=10000.0, accepted=JUNE_18, unit="Central Party"):
    return {"Id": id, "ECRef": f"C{id:07d}", "DonorName": donor, "DonorStatus": status,
            "CompanyRegistrationNumber": number, "RegulatedEntityType": entity_type,
            "RegulatedEntityName": recipient, "RegulatedDoneeType": donee_type, "Value": value,
            "AcceptedDate": accepted, "AccountingUnitName": unit, "DonationType": "Cash",
            "NatureOfDonation": None, "IsSponsorship": False, "RegisterName": "Great Britain"}


def test_dates_and_queries():
    assert str(ec_date(JUNE_18)) == "2026-06-18"  # not the 17th
    assert ec_date(None) is None
    assert query_for("J.C. BAMFORD EXCAVATORS LIMITED") == "j c bamford excavators"
    assert query_for("MARKS AND SPENCER P.L.C.") == "marks spencer"
    assert query_for("Marks & Spencer plc") == "marks spencer"


@respx.mock
def test_donations_matched_on_company_number():
    def search(request):
        q, start = request.url.params["query"], int(request.url.params["start"])
        assert request.url.params["sort"] == "AcceptedDate"  # the API answers -1 without it
        if q == "j c bamford excavators":
            pages = {0: [row(1), row(2, number="00561597", value=5000.0, accepted="/Date(1599951600000)/"),
                         row(3, donor="Bamford Excavators Pension Trust", number="999", value=1.0),  # another company
                         row(8, donor="J.C. BAMFORD EXCAVATORS LTD", number="561598"),  # a typo?
                         row(4, donor="Someone Bamford", status="Individual", number=None)],  # a person
                     2: [row(5, entity_type="Regulated Donee", recipient="Jane Politician",
                             donee_type="MP - Member of Parliament", value=4181.0, unit=None),
                         row(6, entity_type="Regulated Donee", recipient="Stoke Conservative Association",
                             donee_type="Members Association", value=500.0)]}
            # pages of 50: say there are 52 results so a second page is asked for
            return httpx.Response(200, json={"Total": 52, "Result": pages.get(start // 50 * 2, [])})
        if q == "jcb old name":  # a previous name: one donation filed without a number
            return httpx.Response(200, json={"Total": 1, "Result": [
                row(7, donor="JCB OLD NAME LIMITED", number=None, value=250.0)]})
        return httpx.Response(200, json={"Total": 0, "Result": []})
    route = respx.get(SEARCH_URL).mock(side_effect=search)

    pd = fetch_donations("00561597", ["J.C. BAMFORD EXCAVATORS LIMITED", "JCB OLD NAME LIMITED"])
    assert route.call_count == 3  # two pages for the current name, one for the previous name
    assert {d.reference for d in pd.items} == {"C0000001", "C0000002", "C0000005", "C0000006", "C0000007"}
    assert pd.found == 5 and pd.total_value == 19931.0
    assert pd.searched_for == ["j c bamford excavators", "jcb old name"]

    by_ref = {d.reference: d for d in pd.items}
    mp = by_ref["C0000005"]
    # politicians are public figures: named, and flagged as individuals
    assert mp.recipient == "Jane Politician" and mp.to_individual
    assert mp.recipient_type == "MP - Member of Parliament"
    assert by_ref["C0000006"].recipient == "Stoke Conservative Association"  # an organisation
    assert not by_ref["C0000006"].to_individual
    assert "Someone Bamford" not in pd.model_dump_json()  # individual donors are never read
    assert by_ref["C0000007"].matched_by == "name" and any("exact name" in n for n in pd.notes)
    assert any("1 donation(s) under the same name but company number 561598" in n for n in pd.notes)
    assert pd.items[0].accepted_on.isoformat() == "2026-06-18"  # newest first
    assert pd.by_recipient["Conservative and Unionist Party"] == 15250.0
    assert pd.by_recipient["Jane Politician"] == 4181.0
    assert str(pd.first_donation) == "2020-09-13" and pd.items[0].url.endswith("/C0000001")


@respx.mock
def test_no_donations_and_failures():
    respx.get(SEARCH_URL).respond(json={"Total": 0, "Result": []})
    pd = fetch_donations("09461711", ["PLANLOGIC SOFTWARE LIMITED"])
    assert pd.found == 0 and pd.total_value is None and pd.items == []
    respx.get(SEARCH_URL).respond(503)
    assert "failed" in fetch_donations("1", ["X LTD"]).notes[0]


# --- the group ---------------------------------------------------------------------------------


def test_group_companies_from_ownership_and_lei():
    from informant.pipeline import group_companies
    from informant.schema import CompanyProfile, Identity, LegalEntity, LeiEntity, Ownership, ParentCompany

    profile = CompanyProfile(
        query="x", identity=Identity(name="SUB LTD", company_number="01234567"),
        ownership=Ownership(parents=[
            ParentCompany(level=1, name="MID HOLDINGS LIMITED", company_number="02222222"),
            ParentCompany(level=2, name="FOREIGN PARENT INC", company_number=None)]),
        lei=LegalEntity(
            ultimate_parent=LeiEntity(lei="A", name="MID HOLDINGS LIMITED", company_number="2222222"),
            subsidiaries=[LeiEntity(lei="B", name="SUB TWO LIMITED", company_number="03333333"),
                          LeiEntity(lei="C", name="SUB JERSEY LTD", country="JE"),  # not at CH
                          LeiEntity(lei="D", name="SELF", company_number="1234567")]))
    assert group_companies(profile) == [("02222222", "MID HOLDINGS LIMITED", "parent"),
                                        ("03333333", "SUB TWO LIMITED", "subsidiary")]


@respx.mock
def test_group_donations_reported_separately():
    from informant.sources.donations import fetch_group_donations

    def search(request):
        if request.url.params["query"] == "mid holdings":
            return httpx.Response(200, json={"Total": 1, "Result": [
                row(10, donor="Mid Holdings Ltd", number="2222222", value=7500.0, recipient="Labour Party")]})
        return httpx.Response(200, json={"Total": 0, "Result": []})
    respx.get(SEARCH_URL).mock(side_effect=search)
    group, notes = fetch_group_donations([("02222222", "MID HOLDINGS LIMITED", "parent"),
                                          ("03333333", "SUB TWO LIMITED", "subsidiary")])
    assert [(g.name, g.relation, g.found, g.total_value) for g in group] == [
        ("MID HOLDINGS LIMITED", "parent", 1, 7500.0)]  # only those that donated
    assert group[0].by_recipient == {"Labour Party": 7500.0} and notes == []


# --- what the accounts say ----------------------------------------------------------------------


def test_political_statement_from_accounts_text():
    from informant.sources.donations import political_statement

    text = """Directors' report. The directors present their report.
    Political donations
    The company made political donations of £25,000 to the Conservative Party during the year
    (2024: £nil). No political expenditure was incurred. Charitable donations were £3,000."""
    s = political_statement(text)
    assert s.startswith("Political donations The company made political donations of £25,000")
    assert "No political expenditure was incurred." in s and "Charitable" not in s
    assert political_statement("The group's policy on donations to charities is generous.") is None
    assert political_statement("") is None


def test_ixbrl_statement_reaches_the_donations_section():
    from informant.sources.companies_house.accounts import document_text
    from informant.sources.donations import political_statement

    html = (b'<html><body><div>Directors report</div><p>The company made no political '
            b'donations during the year.</p><ix:nonFraction name="core:Turnover">1</ix:nonFraction>'
            b'</body></html>')
    assert political_statement(document_text(html)) == \
        "The company made no political donations during the year."


def test_statement_split_across_ixbrl_tags_stays_whole():
    from informant.sources.companies_house.accounts import document_text
    from informant.sources.donations import political_statement

    html = b'<p>The company made <span>political donations</span> of <b>5,000</b> in the year.</p>'
    assert political_statement(document_text(html)) == \
        "The company made political donations of 5,000 in the year."


def test_stray_ocr_quote_marks_are_removed():
    from informant.sources.donations import political_statement

    text = "Greggs prohibits political donations ‘or contributions. ‘Accordingly, none were made."
    assert political_statement(text) == "Greggs prohibits political donations or contributions."
