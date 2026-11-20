"""HM Land Registry companies dataset: the local SQLite copy, monthly updates, lookups."""

import csv
import io
import json
import zipfile
from datetime import date, datetime, timedelta, timezone

import respx

from informant.changes import compare
from informant.pipeline import research_property
from informant.schema import (
    CompanyProfile,
    Identity,
    LegalEntity,
    LeiEntity,
    PreviousName,
)
from informant.sources import land_registry as lr
from informant.sources.land_registry import PropertyStore, fetch_property, read_rows

HEADER = ["Title Number", "Tenure", "Property Address", "District", "County", "Region", "Postcode",
          "Multiple Address Indicator", "Price Paid"]
for _i in range(1, 5):
    HEADER += [f"Proprietor Name ({_i})", f"Company Registration No. ({_i})",
               f"Proprietorship Category ({_i})"] + [f"Proprietor ({_i}) Address ({j})" for j in (1, 2, 3)]
HEADER += ["Date Proprietor Added", "Additional Proprietor Indicator"]
LTD = "Limited Company or Public Limited Company"


def rec(title, owners, tenure="Freehold", address="1 High Street, Leeds LS1 1AA", region="YORKS AND HUMBER",
        price="", added="01-02-2020", change=None):
    row = {"Title Number": title, "Tenure": tenure, "Property Address": address, "District": "LEEDS",
           "County": "WEST YORKSHIRE", "Region": region, "Postcode": "LS1 1AA",
           "Multiple Address Indicator": "N", "Price Paid": price, "Date Proprietor Added": added,
           "Additional Proprietor Indicator": "N"}
    for i, (name, number) in enumerate(owners, start=1):
        row[f"Proprietor Name ({i})"] = name
        row[f"Company Registration No. ({i})"] = number
        row[f"Proprietorship Category ({i})"] = LTD
        row[f"Proprietor ({i}) Address ({1})"] = "Head Office, Leeds LS2 2BB"
    if change:
        row["Change Indicator"], row["Change Date"] = change, "02-11-2026"
    return row


def make_zip(path, rows, change_file=False):
    header = HEADER + (["Change Indicator", "Change Date"] if change_file else [])
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=header, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    buf.write("Row Count: %d\n" % len(rows))  # the real files end with a footer line
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(path.stem + ".csv", "﻿" + buf.getvalue())
    return path


FULL_ROWS = [
    rec("WYK1", [("ACME LIMITED", "1234567")], price="250000", added="15-03-2019"),
    rec("WYK2", [("ACME LIMITED", "1234567"), ("BIG BANK PLC", "2065")], tenure="Leasehold",
        address="Unit 4, Leeds", added="01-06-2022"),
    rec("WYK3", [("Acme Ltd", "")], address="Old depot, Bradford", price="1,000,000", added="01-01-2001"),
    rec("WYK4", [("OTHER LIMITED", "7654321")]),
    rec("NGL5", [("ACME PARENT LIMITED", "SC111111")], region="GREATER LONDON", price="900000"),
]


def build_store(tmp_path, rows=FULL_ROWS, month="2026-10") -> PropertyStore:
    store = PropertyStore(tmp_path / "ccod.sqlite")
    zip_path = make_zip(tmp_path / f"CCOD_FULL_{month.replace('-', '_')}.zip", rows)
    store.build(read_rows(zip_path), dataset=zip_path.name, month=month, published=date(2026, 10, 2))
    return store


def test_build_and_lookup_by_number_and_exact_name(tmp_path):
    store = build_store(tmp_path)
    assert store.meta()["titles"] == "5"  # the footer line is skipped
    h = fetch_property("01234567", ["ACME LIMITED"], store=store)  # leading zero dropped in the data
    assert h.found == 3 and h.freehold == 2 and h.leasehold == 1
    assert [t.title_number for t in h.items] == ["WYK2", "WYK1", "WYK3"]  # newest first
    assert h.total_price_paid == 1_250_000 and h.priced_titles == 2
    assert h.first_acquired == date(2001, 1, 1) and h.last_acquired == date(2022, 6, 1)
    assert h.regions == {"YORKS AND HUMBER": 3}
    shared = h.items[0]
    assert [c.name for c in shared.co_owners] == ["BIG BANK PLC"]
    assert shared.owner_address == "Head Office, Leeds LS2 2BB"
    assert h.items[2].matched_by == "name"  # no number on that entry, same legal name
    assert any("exact name" in n for n in h.notes)
    assert h.dataset == "CCOD_FULL_2026_10.zip" and h.data_as_of == date(2026, 10, 2)
    assert h.sources[0].name == "land_registry"


def test_initials_written_differently_still_match_exactly(tmp_path):
    store = build_store(tmp_path, rows=[
        rec("SF1", [("J C BAMFORD EXCAVATORS LIMITED", "")]),
        rec("SF2", [("J.C.BAMFORD EXCAVATORS LIMITED", "")]),
        rec("SF3", [("JC BAMFORD EXCAVATORS LIMITED", "")]),
        rec("SF4", [("J C BAMFORD HOLDINGS LIMITED", "")]),
    ])
    assert lr.name_variants("J.C. BAMFORD EXCAVATORS LIMITED") == [
        "jc bamford excavators", "j c bamford excavators"]
    h = fetch_property("00561597", ["J.C. BAMFORD EXCAVATORS LIMITED"], store=store)
    assert sorted(t.title_number for t in h.items) == ["SF1", "SF3"]


def test_a_similar_name_is_not_a_match(tmp_path):
    store = build_store(tmp_path)
    h = fetch_property("09999999", ["ACME HOLDINGS LIMITED"], store=store)
    assert h.found == 0


def test_change_file_deletes_then_adds(tmp_path):
    store = build_store(tmp_path)
    changes = make_zip(tmp_path / "CCOD_COU_2026_11.zip", [
        # a changed title is a deletion of the old record plus an addition of the new one;
        # the addition comes first here to check the order doesn't matter
        rec("WYK1", [("ACME LIMITED", "1234567")], price="300000", added="15-03-2019", change="A"),
        rec("WYK1", [("ACME LIMITED", "1234567")], price="250000", added="15-03-2019", change="D"),
        rec("WYK2", [("ACME LIMITED", "1234567")], change="D"),  # sold
        rec("WYK9", [("ACME LIMITED", "1234567")], added="20-10-2026", change="A"),  # bought
    ], change_file=True)
    added, deleted = store.apply_changes(read_rows(changes), dataset=changes.name, month="2026-11",
                                         published=date(2026, 11, 2))
    assert (added, deleted) == (2, 2)
    h = fetch_property("1234567", store=store)
    assert sorted(t.title_number for t in h.items) == ["WYK1", "WYK9"]
    assert next(t for t in h.items if t.title_number == "WYK1").price_paid == 300_000
    meta = store.meta()
    assert meta["month"] == "2026-11" and meta["changes"] == "CCOD_COU_2026_11.zip"
    assert meta["titles"] == "5"


def test_no_local_copy_says_how_to_get_one(tmp_path):
    h = fetch_property("01234567", store=PropertyStore(tmp_path / "missing.sqlite"))
    assert h.found == 0 and h.dataset is None and "property --update" in h.notes[0]


# --- the API ---------------------------------------------------------------------------------

API = lr.API


def mock_api(tmp_path, latest_month="2026_11", history_months=("2026_10", "2026_11")):
    respx.get(f"{API}/datasets/ccod").respond(json={"success": True, "result": {
        "last_updated": "3 November 2026", "resources": [
            {"file_name": f"CCOD_COU_{latest_month}.zip"}, {"file_name": f"CCOD_FULL_{latest_month}.zip"}]}})
    respx.get(f"{API}/datasets/history/ccod").respond(json={"dataset": "ccod", "dataset_history": [
        {"filename": f"CCOD_{kind}_{m}.zip", "unsorted_date": f"02-{m[5:]}-{m[:4]}"}
        for m in history_months for kind in ("COU", "FULL")]})
    change = make_zip(tmp_path / "dl_COU.zip", [
        rec("WYK9", [("ACME LIMITED", "1234567")], added="20-10-2026", change="A")], change_file=True)
    full = make_zip(tmp_path / "dl_FULL.zip", FULL_ROWS)
    for kind, path in (("COU", change), ("FULL", full)):
        name = f"CCOD_{kind}_{latest_month}.zip"
        respx.get(f"{API}/datasets/ccod/{name}").respond(json={"success": True, "result": {
            "download_url": f"https://bucket.example/ccod/{name}?X-Amz-Signature=abc",
            "valid_for_seconds": 10}})
    s3_change = respx.get(f"https://bucket.example/ccod/CCOD_COU_{latest_month}.zip").respond(
        content=change.read_bytes())
    s3_full = respx.get(f"https://bucket.example/ccod/CCOD_FULL_{latest_month}.zip").respond(
        content=full.read_bytes())
    return s3_change, s3_full


@respx.mock
def test_update_applies_the_next_change_file(tmp_path, monkeypatch):
    monkeypatch.setenv("INFORMANT_DATA_DIR", str(tmp_path / "data"))
    store = build_store(tmp_path)
    s3_change, s3_full = mock_api(tmp_path)
    result = lr.update(store, lr.LandRegistryApi(key="secret-key"))
    assert result == {"action": "changes", "month": "2026-11", "files": ["CCOD_COU_2026_11.zip"]}
    assert not s3_full.called
    # the signed storage link must never receive the API key
    assert "authorization" not in {k.lower() for k in s3_change.calls[0].request.headers}
    assert fetch_property("1234567", store=store).found == 3  # WYK1, WYK2, WYK9
    assert not list((tmp_path / "data" / "downloads").glob("*.zip"))  # cleaned up
    assert lr.update(store, lr.LandRegistryApi(key="k"))["action"] == "none"


@respx.mock
def test_update_needs_the_full_file_after_a_gap(tmp_path, monkeypatch):
    monkeypatch.setenv("INFORMANT_DATA_DIR", str(tmp_path / "data"))
    store = build_store(tmp_path, month="2026-08")  # September's change file isn't listed
    mock_api(tmp_path, history_months=("2026_10", "2026_11"))
    assert lr.update(store, lr.LandRegistryApi(key="k"), allow_full=False)["action"] == "needs_full"
    result = lr.update(store, lr.LandRegistryApi(key="k"))
    assert result["action"] == "full" and result["titles"] == 5
    assert store.meta()["month"] == "2026-11"


@respx.mock
def test_first_update_downloads_the_full_file(tmp_path, monkeypatch):
    monkeypatch.setenv("INFORMANT_DATA_DIR", str(tmp_path / "data"))
    store = PropertyStore(tmp_path / "data" / "ccod.sqlite")
    mock_api(tmp_path)
    assert lr.update(store, lr.LandRegistryApi(key="k"))["action"] == "full"
    assert fetch_property("1234567", store=store).found == 2


@respx.mock
def test_a_refused_key_is_a_config_error(tmp_path):
    respx.get(f"{API}/datasets/ccod").respond(status_code=401)
    try:
        lr.update(PropertyStore(tmp_path / "x.sqlite"), lr.LandRegistryApi(key="bad"))
    except lr.ConfigError as e:
        assert "LAND_REGISTRY_API_KEY" in str(e)
    else:
        raise AssertionError("expected ConfigError")


def test_lookups_check_at_most_once_a_day(tmp_path, monkeypatch):
    store = build_store(tmp_path)  # checked_at = now
    monkeypatch.setenv("LAND_REGISTRY_API_KEY", "k")
    calls = []
    monkeypatch.setattr(lr, "update", lambda *a, **k: calls.append(k) or {"action": "none"})
    assert lr.keep_current(store) is None and calls == []
    with store.session() as conn:
        store.set_meta(conn, checked_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat())
    lr.keep_current(store)
    assert calls == [{"full": False, "allow_full": False, "notify": None}]  # never the full file


# --- the pipeline, CLI and change tracking ------------------------------------------------------


def profile(**extra) -> CompanyProfile:
    return CompanyProfile(query="x", identity=Identity(
        name="ACME LIMITED", company_number="01234567", status="active",
        previous_names=[PreviousName(name="OLD ACME LIMITED")]), **extra)


def test_research_property_with_group(tmp_path):
    store = build_store(tmp_path)
    p = profile(lei=LegalEntity(lei="X", ultimate_parent=LeiEntity(
        lei="Y", name="ACME PARENT LIMITED", country="GB", company_number="SC111111")))
    page = research_property(p, store=store)
    assert p.property.found == 3
    assert [(g.name, g.titles, g.relation) for g in p.property.group] == [("ACME PARENT LIMITED", 1, "parent")]
    assert p.property.group[0].total_price_paid == 900_000
    assert page.kind == "property" and "3 registered title(s)" in page.text


def test_property_changes_between_lookups(tmp_path):
    store = build_store(tmp_path)
    old = profile()
    research_property(old, store=store)
    new = old.model_copy(deep=True)
    new.generated_at = old.generated_at + timedelta(days=40)
    new.property.items = [t for t in new.property.items if t.title_number != "WYK2"]
    new.property.found = 2
    got = [f"{c.kind}|{c.summary}" for c in compare(old, new).changes]
    assert "changed|titles owned: 3 -> 2" in got
    assert "removed|title no longer registered to it: Leasehold Unit 4, Leeds" in got
    # a lookup without the dataset isn't compared
    bare = profile()
    assert "property" in compare(old, bare).not_compared


def test_cli_status_and_company(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from informant.cli import app

    monkeypatch.chdir(tmp_path)  # no .env here: the user's real key must not be read
    monkeypatch.setenv("INFORMANT_DATA_DIR", str(tmp_path))
    runner = CliRunner()
    empty = json.loads(runner.invoke(app, ["property", "--json"]).stdout)
    assert empty["ok"] and empty["exists"] is False and empty["api_key_set"] is False
    missing = runner.invoke(app, ["property", "01234567", "--json"])
    assert missing.exit_code == 2
    no_key = runner.invoke(app, ["property", "--update", "--json"])
    assert no_key.exit_code == 3 and json.loads(no_key.stdout)["error"]["code"] == "config_error"

    build_store(tmp_path).path.rename(lr.db_path())
    status = json.loads(runner.invoke(app, ["property", "--json"]).stdout)
    assert status["titles"] == 5 and status["month"] == "2026-10"
    data = json.loads(runner.invoke(app, ["property", "1234567", "--json"]).stdout)
    assert data["company_number"] == "01234567" and data["property"]["found"] == 2
    text = runner.invoke(app, ["property", "1234567"]).stdout
    assert "2 title(s)" in text and "Unit 4, Leeds" in text
