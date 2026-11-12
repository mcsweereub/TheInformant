"""The machine-facing contract: --json output, exit codes, progress, index."""

import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from informant import cli
from informant.sources.companies_house import BASE_URL
from informant.pipeline import research_company
from informant.storage import INDEX_NAME, load_index
from test_companies_house import FILINGS, OFFICERS, PROFILE, PSCS

NUMBER = "01234567"


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "test-key")
    monkeypatch.delenv("INFORMANT_OUTPUT_DIR", raising=False)
    return tmp_path


def mock_companies_house(search_items=None):
    respx.get(f"{BASE_URL}/company/{NUMBER}").respond(json=PROFILE)
    respx.get(f"{BASE_URL}/company/{NUMBER}/officers").respond(json=OFFICERS)
    respx.get(f"{BASE_URL}/company/{NUMBER}/persons-with-significant-control").respond(json=PSCS)
    respx.get(f"{BASE_URL}/company/{NUMBER}/filing-history").respond(json=FILINGS)
    respx.get(f"{BASE_URL}/search/companies").respond(json={"items": search_items or []})


def invoke(*args):
    return CliRunner().invoke(cli.app, list(args))


def parse(result):
    lines = [l for l in result.stdout.splitlines() if l.strip()]
    assert len(lines) == 1, f"stdout must be exactly one JSON line, got: {result.stdout!r}"
    return json.loads(lines[0])


@respx.mock
def test_lookup_json_saves_profile_and_index(workdir):
    mock_companies_house()
    result = invoke("lookup", NUMBER, "--no-web", "--json")
    assert result.exit_code == 0, result.output
    data = parse(result)

    assert data["ok"] and data["command"] == "lookup"
    assert data["matched_by"] == "number" and data["company_number"] == NUMBER
    assert data["profile"]["identity"]["name"] == "EXAMPLE TRADING LIMITED"
    path = workdir / "output" / "01234567-example-trading-limited.json"
    assert data["path"] == str(path.resolve())
    assert json.loads(path.read_text())["identity"]["company_number"] == NUMBER

    index = json.loads((workdir / "output" / INDEX_NAME).read_text())
    assert [c["company_number"] for c in index["companies"]] == [NUMBER]
    assert index["companies"][0]["file"] == path.name
    assert not list((workdir / "output").glob("*.tmp"))  # atomic writes leave no temp files


@respx.mock
def test_relookup_replaces_index_entry(workdir):
    mock_companies_house()
    invoke("lookup", NUMBER, "--no-web", "--json")
    invoke("lookup", NUMBER, "--no-web", "--json")
    index = load_index(workdir / "output")
    assert len(index["companies"]) == 1


@respx.mock
def test_lookup_by_name_reports_alternatives(workdir):
    mock_companies_house(search_items=[
        {"company_number": NUMBER, "title": "EXAMPLE TRADING LIMITED", "company_status": "active"},
        {"company_number": "07654321", "title": "EXAMPLE TRADING (NORTH) LTD", "company_status": "dissolved"},
    ])
    data = parse(invoke("lookup", "example trading", "--no-web", "--json"))
    assert data["matched_by"] == "search"
    assert data["alternatives"][0]["company_number"] == "07654321"


@respx.mock
def test_progress_events_on_stderr(workdir):
    mock_companies_house()
    result = invoke("lookup", NUMBER, "--no-web", "--json", "--progress")
    assert result.exit_code == 0
    events = [json.loads(l) for l in result.stderr.splitlines() if l.strip()]
    assert all(e["event"] == "progress" for e in events)
    seen = [(e["stage"], e["status"]) for e in events]
    assert seen[0] == ("resolve", "started")
    assert ("companies_house", "done") in seen
    assert ("website_discovery", "skipped") in seen
    assert seen[-1] == ("save", "done")
    parse(result)  # stdout is still exactly one JSON result


@respx.mock
def test_out_file_and_out_conflict(workdir):
    mock_companies_house()
    target = workdir / "elsewhere" / "acme.json"
    data = parse(invoke("lookup", NUMBER, "--no-web", "--json", "--out-file", str(target)))
    assert data["path"] == str(target.resolve()) and target.exists()
    assert (target.parent / INDEX_NAME).exists()

    result = invoke("lookup", NUMBER, "--json", "--out", "a", "--out-file", "b.json")
    assert result.exit_code == 3 and parse(result)["error"]["code"] == "config_error"


@respx.mock
def test_output_dir_from_env(workdir, monkeypatch):
    mock_companies_house()
    monkeypatch.setenv("INFORMANT_OUTPUT_DIR", str(workdir / "profiles"))
    data = parse(invoke("lookup", NUMBER, "--no-web", "--json"))
    assert data["path"].startswith(str((workdir / "profiles").resolve()))


@respx.mock
def test_exit_codes(workdir, monkeypatch):
    mock_companies_house(search_items=[])
    result = invoke("lookup", "no such company xyz", "--no-web", "--json")
    assert result.exit_code == 2 and parse(result)["error"]["code"] == "not_found"

    respx.get(f"{BASE_URL}/company/09999999").respond(404)
    result = invoke("lookup", "09999999", "--no-web", "--json")
    assert result.exit_code == 2

    respx.get(f"{BASE_URL}/company/08888888").mock(side_effect=httpx.ConnectError("offline"))
    result = invoke("lookup", "08888888", "--no-web", "--json")
    assert result.exit_code == 4 and parse(result)["error"]["code"] == "upstream_error"

    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "")
    result = invoke("lookup", NUMBER, "--no-web", "--json")
    assert result.exit_code == 3 and parse(result)["error"]["code"] == "config_error"


@respx.mock
def test_search_json(workdir):
    mock_companies_house(search_items=[
        {"company_number": NUMBER, "title": "EXAMPLE TRADING LIMITED", "company_status": "active",
         "address_snippet": "1 High Street, London", "date_of_creation": "2010-03-15"}])
    data = parse(invoke("search", "example", "--json"))
    assert data["results"][0] == {
        "company_number": NUMBER, "title": "EXAMPLE TRADING LIMITED", "status": "active",
        "address": "1 High Street, London", "date_of_creation": "2010-03-15"}


@respx.mock
def test_list_json_and_index_rebuild(workdir):
    mock_companies_house()
    invoke("lookup", NUMBER, "--no-web", "--json")
    (workdir / "output" / INDEX_NAME).unlink()  # lost index is rebuilt from the files
    data = parse(invoke("list", "--json"))
    assert [c["name"] for c in data["companies"]] == ["EXAMPLE TRADING LIMITED"]
    assert (workdir / "output" / INDEX_NAME).exists()


def test_list_empty_folder(workdir):
    data = parse(invoke("list", "--json"))
    assert data["ok"] and data["companies"] == []


def test_version_json():
    data = parse(invoke("version", "--json"))
    assert data["schema_version"] and data["version"]


@respx.mock
def test_research_company_as_library(workdir):
    mock_companies_house()
    events = []
    result = research_company(NUMBER, web=False, progress=events.append)
    assert result.profile.identity.company_number == NUMBER
    assert events[0].stage == "resolve" and events[-1].stage == "llm"
