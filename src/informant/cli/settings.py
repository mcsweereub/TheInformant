"""Settings and information: model choice, schema, version."""

from __future__ import annotations

import json
import os
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path

import typer
from dotenv import set_key
from rich.table import Table

from ..errors import ConfigError
from ..llm import ModelOption, current_model, model_options
from ..schema import SCHEMA_VERSION, CompanyProfile
from .common import (
    JSON_OPT,
    STATE,
    _run,
    app,
    out,
    print_json,
)


def save_model_choice(option: ModelOption, env_file: Path | None = None) -> Path:
    """Persist the chosen backend + model to the settings file (and this process)."""
    env_file = env_file or STATE.env_file
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.touch(exist_ok=True)
    key = "INFORMANT_MODEL" if option.backend == "claude" else "OLLAMA_MODEL"
    for k, v in (("INFORMANT_LLM", option.backend), (key, option.model)):
        set_key(str(env_file), k, v, quote_mode="never")
        os.environ[k] = v
    return env_file


@app.command("model")
def model_cmd(
    choice: str | None = typer.Argument(
        None, help="Number from the list, or a model name like claude-haiku-5-5 or qwen3:4b."),
    list_only: bool = typer.Option(False, "--list", "-l", help="Just show the list."),
    json_mode: bool = JSON_OPT,
) -> None:
    """Choose which LLM extracts website data (Claude models or local Ollama models)."""

    def body() -> None:
        nonlocal choice
        options, ollama_up = model_options()
        cur_backend, cur_model = current_model()

        if json_mode and (list_only or choice is None):
            print_json({
                "ok": True, "command": "model",
                "current": {"backend": cur_backend, "model": cur_model},
                "ollama_running": ollama_up,
                "models": [
                    {"number": i, "backend": o.backend, "model": o.model, "notes": o.notes,
                     "current": (o.backend, o.model) == (cur_backend, cur_model)}
                    for i, o in enumerate(options, 1)
                ],
            })
            return

        if not json_mode:
            table = Table("#", "Backend", "Model", "Notes", title="LLM models")
            for i, opt in enumerate(options, 1):
                is_current = (opt.backend, opt.model) == (cur_backend, cur_model)
                mark = "[green]* [/green]" if is_current else ""
                table.add_row(str(i), opt.backend, f"{mark}{opt.model}", opt.notes,
                              style="bold" if is_current else None)
            out.print(table)
            if not ollama_up:
                out.print("[dim]Ollama isn't running, so local models aren't listed. "
                          "Start the Ollama app to see them.[/dim]")
            out.print(f"Current: [bold]{cur_backend}/{cur_model}[/bold]")
            if list_only:
                return
            if choice is None:
                choice = typer.prompt("Choose a number (Enter to keep current)", default="",
                                      show_default=False)
                if not choice.strip():
                    return

        assert choice is not None
        c = choice.strip()
        if c.isdigit() and 1 <= int(c) <= len(options):
            selected = options[int(c) - 1]
        else:
            selected = next((o for o in options if o.model == c), None)
        if selected is None:
            raise ConfigError(f"'{choice}' isn't in the model list.")

        env_file = save_model_choice(selected)
        warnings = []
        if selected.backend == "claude" and not os.environ.get("ANTHROPIC_API_KEY"):
            warnings.append("ANTHROPIC_API_KEY is not set; add it before running a lookup.")
        if json_mode:
            print_json({"ok": True, "command": "model",
                        "saved": {"backend": selected.backend, "model": selected.model},
                        "env_file": str(env_file.resolve()), "warnings": warnings})
            return
        out.print(f"[green]Saved: {selected.backend}/{selected.model} (in {env_file})[/green]")
        for w in warnings:
            out.print(f"[yellow]{w}[/yellow]")

    _run("model", json_mode, body)


@app.command()
def schema() -> None:
    """Print the JSON Schema of the output format (for downstream programs)."""
    print(json.dumps(CompanyProfile.model_json_schema(), indent=2))


@app.command()
def version(json_mode: bool = JSON_OPT) -> None:
    """Show the program and output-schema versions."""
    try:
        v = pkg_version("informant")
    except PackageNotFoundError:
        v = "unknown"
    if json_mode:
        print_json({"ok": True, "command": "version", "version": v, "schema_version": SCHEMA_VERSION})
    else:
        out.print(f"informant {v} (output schema {SCHEMA_VERSION})")
