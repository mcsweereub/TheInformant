"""Command line interface.

People get readable output. Programs (e.g. a GUI) add `--json` to any command:
  * stdout carries exactly one JSON object: {"ok": true, ...} or
    {"ok": false, "error": {"code": ..., "message": ...}}
  * nothing ever prompts for input
  * the exit code says what happened (see errors.py): 0 ok, 1 internal error,
    2 not found, 3 config error, 4 upstream error
`lookup --progress` also writes one JSON progress event per line to stderr.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version as pkg_version
from pathlib import Path
from typing import Any

import typer
from dotenv import load_dotenv, set_key
from rich.console import Console
from rich.table import Table

from .companies_house import CompaniesHouseClient
from .errors import ConfigError, InformantError
from .llm import ModelOption, choose_backend, current_model, model_options
from .pipeline import research_company
from .progress import ProgressEvent, emit
from .schema import SCHEMA_VERSION, CompanyProfile
from .storage import default_output_dir, load_index, save_profile

app = typer.Typer(add_completion=False, help="Research UK companies into structured JSON.")
out = Console()  # results for people (stdout)
err = Console(stderr=True)  # status, notes and errors for people (stderr)


class _State:
    env_file: Path = Path(".env")


STATE = _State()

JSON_OPT = typer.Option(False, "--json", help="Machine-readable output: one JSON object on stdout.")


@app.callback()
def main(
    env_file: Path | None = typer.Option(
        None, "--env-file", envvar="INFORMANT_ENV_FILE",
        help="Settings file to use instead of ./.env (API keys, model, output folder)."),
) -> None:
    STATE.env_file = env_file or Path(".env")
    if STATE.env_file.is_file():
        load_dotenv(STATE.env_file, override=False)


# --- helpers ------------------------------------------------------------------


def print_json(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def _run(command: str, json_mode: bool, body: Callable[[], None]) -> None:
    """Run a command body, turning errors into the right output and exit code."""
    try:
        body()
    except typer.Exit:
        raise
    except InformantError as e:
        if json_mode:
            print_json({"ok": False, "command": command,
                        "error": {"code": e.code, "message": str(e)}})
        else:
            err.print(f"[red]{e}[/red]")
        raise typer.Exit(e.exit_code)
    except Exception as e:  # unexpected: still give programs a parseable answer
        if not json_mode:
            raise
        print_json({"ok": False, "command": command,
                    "error": {"code": "internal_error", "message": f"{type(e).__name__}: {e}"}})
        raise typer.Exit(1)


def _ch_client() -> CompaniesHouseClient:
    return CompaniesHouseClient(os.environ.get("COMPANIES_HOUSE_API_KEY", ""))


def _write_progress_line(event: ProgressEvent) -> None:
    sys.stderr.write(json.dumps(event.to_dict(), default=str) + "\n")
    sys.stderr.flush()


# --- search -------------------------------------------------------------------


@app.command()
def search(
    query: str,
    limit: int = typer.Option(10, "--limit", "-n"),
    json_mode: bool = JSON_OPT,
) -> None:
    """List Companies House matches for a name."""

    def body() -> None:
        with _ch_client() as ch:
            hits = ch.search(query, limit=limit)
        if json_mode:
            print_json({"ok": True, "command": "search", "query": query,
                        "results": [asdict(h) for h in hits]})
            return
        table = Table("Number", "Name", "Status", "Incorporated", "Address")
        for h in hits:
            table.add_row(h.company_number, h.title, h.status or "", h.date_of_creation or "", h.address or "")
        out.print(table)

    _run("search", json_mode, body)


# --- lookup -------------------------------------------------------------------


@app.command()
def lookup(
    query: str = typer.Argument(..., help="Company number, or a name (top search hit is used)."),
    out_dir: Path | None = typer.Option(
        None, "--out", "-o", help="Folder for the JSON file [default: INFORMANT_OUTPUT_DIR or ./output]."),
    out_file: Path | None = typer.Option(None, "--out-file", help="Exact path for the JSON file."),
    filings: int = typer.Option(25, help="How many recent filings to include."),
    website: str | None = typer.Option(None, "--website", "-w", help="Company website, if you know it."),
    web: bool = typer.Option(True, "--web/--no-web", help="Discover and crawl the company website."),
    llm: bool = typer.Option(True, "--llm/--no-llm", help="Use an LLM to extract products, sectors etc."),
    max_pages: int = typer.Option(8, help="Maximum website pages to fetch."),
    browser: str | None = typer.Option(
        None, "--browser", help="Headless browser for JavaScript-built sites: auto, always or never "
        "[default: INFORMANT_BROWSER or auto]."),
    json_mode: bool = JSON_OPT,
    progress: bool = typer.Option(False, "--progress", help="Write JSON progress events to stderr."),
) -> None:
    """Build a CompanyProfile JSON for one company and save it."""

    def body() -> None:
        if out_dir is not None and out_file is not None:
            raise ConfigError("Use --out or --out-file, not both.")
        if browser is not None and browser.lower() not in ("auto", "always", "never"):
            raise ConfigError("--browser must be auto, always or never.")
        kwargs = dict(website=website, web=web, llm=llm, max_pages=max_pages, filings=filings,
                      browser=browser)

        if progress or json_mode:
            callback = _write_progress_line if progress else None
            result = research_company(query, progress=callback, **kwargs)
        else:
            with err.status("Starting...") as spinner:
                def callback(event: ProgressEvent) -> None:
                    if event.status == "started" and event.message:
                        text = event.message
                        if event.stage == "llm" and choose_backend() == "ollama":
                            text += " (local model: this can take a few minutes)"
                        spinner.update(text + "...")
                result = research_company(query, progress=callback, **kwargs)
            callback = None

        emit(callback, "save", "started", "Saving profile")
        path, index_path = save_profile(result.profile, out_dir=out_dir, out_file=out_file)
        emit(callback, "save", "done", str(path), path=str(path))

        profile = result.profile
        if json_mode:
            print_json({
                "ok": True,
                "command": "lookup",
                "query": query,
                "matched_by": result.matched_by,
                "company_number": profile.identity.company_number,
                "name": profile.identity.name,
                "status": profile.identity.status,
                "website": profile.identity.website,
                "path": str(path),
                "index": str(index_path),
                "alternatives": [asdict(h) for h in result.alternatives],
                "warnings": profile.website.notes,
                "profile": profile.model_dump(mode="json"),
            })
            return

        if result.matched_by == "search":
            err.print(f"Using top match: [bold]{profile.identity.name}[/bold] "
                      f"({profile.identity.company_number})")
            for h in result.alternatives:
                err.print(f"  [dim]also: {h.title} ({h.company_number}, {h.status})[/dim]")
        if web:
            _print_website(profile)
        active = sum(1 for o in profile.people.officers if o.active)
        out.print(
            f"[green]Wrote {path}[/green]  "
            f"status={profile.identity.status}, active officers={active}, "
            f"PSCs={len(profile.people.significant_control)}, filings={len(profile.filings.recent)}"
        )

    _run("lookup", json_mode, body)


def _print_website(profile: CompanyProfile) -> None:
    site = profile.website
    if not site.url:
        out.print("[yellow]Website: not found[/yellow]")
    else:
        out.print(
            f"Website: [bold]{site.url}[/bold] (via {site.discovered_via}, "
            f"confidence {site.confidence}); {sum(1 for p in site.pages if p.text_chars)} pages read"
        )
        if profile.identity.description:
            out.print(f"  [italic]{profile.identity.description}[/italic] [dim]({site.llm_model})[/dim]")
        if profile.classification.sectors:
            out.print(f"  sectors: {', '.join(profile.classification.sectors)}")
        if profile.products:
            out.print(f"  products: {', '.join(p.name for p in profile.products[:8])}")
        if profile.tech_stack:
            out.print(f"  tech: {', '.join(profile.tech_stack)}")
        if site.social_links:
            out.print(f"  social: {', '.join(site.social_links)}")
    for note in site.notes:
        out.print(f"  [dim]note: {note}[/dim]")


# --- list ---------------------------------------------------------------------


@app.command("list")
def list_cmd(
    out_dir: Path | None = typer.Option(
        None, "--out", "-o", help="Folder to list [default: INFORMANT_OUTPUT_DIR or ./output]."),
    json_mode: bool = JSON_OPT,
) -> None:
    """List saved company profiles (from the folder's index.json)."""

    def body() -> None:
        folder = (out_dir or default_output_dir()).resolve()
        index = load_index(folder)
        if json_mode:
            print_json({"ok": True, "command": "list", "folder": str(folder),
                        "updated_at": index.get("updated_at"), "companies": index["companies"]})
            return
        table = Table("Number", "Name", "Status", "Sectors", "Generated", title=str(folder))
        table.add_column("File", overflow="fold")
        for c in index["companies"]:
            table.add_row(c.get("company_number") or "", c.get("name") or "", c.get("status") or "",
                          ", ".join(c.get("sectors") or []), (c.get("generated_at") or "")[:10],
                          c.get("file") or "")
        out.print(table)

    _run("list", json_mode, body)


# --- model --------------------------------------------------------------------


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


# --- schema / version ---------------------------------------------------------


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


if __name__ == "__main__":
    app()
