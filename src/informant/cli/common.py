"""Shared CLI pieces: the typer app, consoles, the --json contract and helpers."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import typer
from dotenv import load_dotenv
from rich.console import Console

from ..errors import InformantError
from ..llm import choose_backend
from ..progress import ProgressEvent
from ..sources.companies_house import CompaniesHouseClient

app = typer.Typer(add_completion=False, help="Research UK companies into structured JSON.")
out = Console()  # results for people (stdout)
err = Console(stderr=True)  # status, notes and errors for people (stderr)
JSON_OPT = typer.Option(False, "--json", help="Machine-readable output: one JSON object on stdout.")


class _State:
    env_file: Path = Path(".env")


STATE = _State()


@app.callback()
def main(
    env_file: Path | None = typer.Option(
        None, "--env-file", envvar="INFORMANT_ENV_FILE",
        help="Settings file to use instead of ./.env (API keys, model, output folder)."),
) -> None:
    STATE.env_file = env_file or Path(".env")
    if STATE.env_file.is_file():
        load_dotenv(STATE.env_file, override=False)


def print_json(obj: dict[str, Any]) -> None:
    # Always UTF-8: on Windows a piped stdout defaults to the console code page (cp1252),
    # which can't encode characters such as "→" in a headline and would crash the output.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError, OSError):
        pass
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
        raise typer.Exit(e.exit_code) from None
    except Exception as e:  # unexpected: still give programs a parseable answer
        if not json_mode:
            raise
        print_json({"ok": False, "command": command,
                    "error": {"code": "internal_error", "message": f"{type(e).__name__}: {e}"}})
        raise typer.Exit(1) from None


def _ch_client() -> CompaniesHouseClient:
    return CompaniesHouseClient(os.environ.get("COMPANIES_HOUSE_API_KEY", ""))


def _write_progress_line(event: ProgressEvent) -> None:
    sys.stderr.write(json.dumps(event.to_dict(), default=str) + "\n")
    sys.stderr.flush()


@contextmanager
def _progress(progress: bool, json_mode: bool):
    """Yields the progress callback: JSON lines on stderr (--progress), nothing (--json),
    or a spinner for people."""
    if progress or json_mode:
        yield _write_progress_line if progress else None
        return
    with err.status("Starting...") as spinner:
        def callback(event: ProgressEvent) -> None:
            if event.status in ("started", "progress") and event.message:
                text = event.message
                if event.stage == "llm" and choose_backend() == "ollama":
                    text += " (local model: this can take a few minutes)"
                spinner.update(text + "...")
        yield callback
