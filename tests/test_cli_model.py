import os

import pytest
from typer.testing import CliRunner

from informant import cli
from informant.llm import ModelOption

OPTIONS = [
    ModelOption("claude", "claude-opus-5-5", "most capable"),
    ModelOption("claude", "claude-haiku-5-5", "cheapest"),
    ModelOption("ollama", "gemma4:12b", "free, local"),
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for k in ("INFORMANT_LLM", "INFORMANT_MODEL", "OLLAMA_MODEL", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(cli, "model_options", lambda: (OPTIONS, True))
    (tmp_path / ".env").write_text("COMPANIES_HOUSE_API_KEY=abc\n")
    yield tmp_path
    for k in ("INFORMANT_LLM", "INFORMANT_MODEL", "OLLAMA_MODEL"):
        os.environ.pop(k, None)


def test_pick_by_number_saves_to_env(env):
    result = CliRunner().invoke(cli.app, ["model"], input="2\n")
    assert result.exit_code == 0, result.output
    text = (env / ".env").read_text()
    assert "COMPANIES_HOUSE_API_KEY=abc" in text  # existing keys kept
    assert "INFORMANT_LLM=claude" in text and "INFORMANT_MODEL=claude-haiku-5-5" in text
    assert "ANTHROPIC_API_KEY" in result.output  # warns that a key is needed


def test_pick_by_name(env):
    result = CliRunner().invoke(cli.app, ["model", "gemma4:12b"])
    assert result.exit_code == 0, result.output
    text = (env / ".env").read_text()
    assert "INFORMANT_LLM=ollama" in text and "OLLAMA_MODEL=gemma4:12b" in text


def test_list_only_and_enter_keep_current(env):
    assert CliRunner().invoke(cli.app, ["model", "--list"]).exit_code == 0
    assert CliRunner().invoke(cli.app, ["model"], input="\n").exit_code == 0
    assert "INFORMANT_LLM" not in (env / ".env").read_text()


def test_invalid_choice(env):
    result = CliRunner().invoke(cli.app, ["model", "9"])
    assert result.exit_code == 3  # config_error


def test_model_json_list_never_prompts(env):
    import json
    result = CliRunner().invoke(cli.app, ["model", "--json"])  # no input supplied
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["ok"] and data["ollama_running"] is True
    assert [m["number"] for m in data["models"]] == [1, 2, 3]
    assert "INFORMANT_LLM" not in (env / ".env").read_text()


def test_model_json_set_and_error(env):
    import json
    result = CliRunner().invoke(cli.app, ["model", "3", "--json"])
    data = json.loads(result.stdout)
    assert data["saved"] == {"backend": "ollama", "model": "gemma4:12b"}
    result = CliRunner().invoke(cli.app, ["model", "nope", "--json"])
    assert result.exit_code == 3
    assert json.loads(result.stdout)["error"]["code"] == "config_error"


def test_env_file_option(env, tmp_path):
    settings = tmp_path / "config" / "informant.env"
    result = CliRunner().invoke(cli.app, ["--env-file", str(settings), "model", "gemma4:12b"])
    assert result.exit_code == 0, result.output
    assert "OLLAMA_MODEL=gemma4:12b" in settings.read_text()
    assert "INFORMANT_LLM" not in (env / ".env").read_text()
