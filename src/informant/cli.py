"""Command line entry point: `informant search ...` / `informant lookup ...`."""

from __future__ import annotations

import os
import re
from pathlib import Path

import typer
from dotenv import load_dotenv, set_key
from rich.console import Console
from rich.table import Table

from .companies_house import (
    CompaniesHouseClient,
    CompaniesHouseError,
    fetch_company,
    normalise_company_number,
)
from .crawler import Crawler
from .llm import ModelOption, choose_backend, current_model, model_options
from .schema import CompanyProfile
from .website import research_website

app = typer.Typer(add_completion=False, help="Research UK companies into structured JSON.")
console = Console()


def _client() -> CompaniesHouseClient:
    load_dotenv()
    return CompaniesHouseClient(os.environ.get("COMPANIES_HOUSE_API_KEY", ""))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


@app.command()
def search(query: str, limit: int = typer.Option(10, "--limit", "-n")) -> None:
    """List Companies House matches for a name."""
    try:
        with _client() as ch:
            hits = ch.search(query, limit=limit)
    except CompaniesHouseError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    table = Table("Number", "Name", "Status", "Incorporated", "Address")
    for h in hits:
        table.add_row(h.company_number, h.title, h.status or "", h.date_of_creation or "", h.address or "")
    console.print(table)


@app.command()
def lookup(
    query: str = typer.Argument(..., help="Company number, or a name (top search hit is used)."),
    out_dir: Path = typer.Option(Path("output"), "--out", "-o", help="Directory for JSON output."),
    filings: int = typer.Option(25, help="How many recent filings to include."),
    website: str | None = typer.Option(None, "--website", "-w", help="Company website, if you know it."),
    web: bool = typer.Option(True, "--web/--no-web", help="Discover and crawl the company website."),
    llm: bool = typer.Option(True, "--llm/--no-llm", help="Use Claude to extract products, sectors etc."),
    max_pages: int = typer.Option(8, help="Maximum website pages to fetch."),
) -> None:
    """Build a CompanyProfile JSON for one company."""
    try:
        with _client() as ch:
            number = normalise_company_number(query)
            if number is None:
                hits = ch.search(query, limit=5)
                if not hits:
                    console.print(f"[red]No Companies House match for {query!r}.[/red]")
                    raise typer.Exit(1)
                number = hits[0].company_number
                console.print(f"Using top match: [bold]{hits[0].title}[/bold] ({number})")
                for h in hits[1:]:
                    console.print(f"  [dim]also: {h.title} ({h.company_number}, {h.status})[/dim]")

            ch_data = fetch_company(ch, number, filings_limit=filings)
    except CompaniesHouseError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    profile = CompanyProfile(
        query=query,
        identity=ch_data.identity,
        classification=ch_data.classification,
        locations=ch_data.locations,
        people=ch_data.people,
        filings=ch_data.filings,
    )

    if web:
        status = "Researching website..."
        if llm and choose_backend() == "ollama":
            status += " (local model: this can take a few minutes)"
        with console.status(status), Crawler() as crawler:
            research_website(profile, crawler=crawler, user_url=website,
                             max_pages=max_pages, use_llm=llm)
        _print_website(profile)

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{number}-{_slug(profile.identity.name)}.json"
    path.write_text(profile.to_json(), encoding="utf-8")

    active = sum(1 for o in profile.people.officers if o.active)
    console.print(
        f"[green]Wrote {path}[/green]  "
        f"status={profile.identity.status}, active officers={active}, "
        f"PSCs={len(profile.people.significant_control)}, filings={len(profile.filings.recent)}"
    )


def _print_website(profile: CompanyProfile) -> None:
    site = profile.website
    if not site.url:
        console.print("[yellow]Website: not found[/yellow]")
    else:
        console.print(
            f"Website: [bold]{site.url}[/bold] (via {site.discovered_via}, "
            f"confidence {site.confidence}); {sum(1 for p in site.pages if p.text_chars)} pages read"
        )
        if profile.identity.description:
            console.print(f"  [italic]{profile.identity.description}[/italic] [dim]({site.llm_model})[/dim]")
        if profile.classification.sectors:
            console.print(f"  sectors: {', '.join(profile.classification.sectors)}")
        if profile.products:
            console.print(f"  products: {', '.join(p.name for p in profile.products[:8])}")
        if profile.tech_stack:
            console.print(f"  tech: {', '.join(profile.tech_stack)}")
        if site.social_links:
            console.print(f"  social: {', '.join(site.social_links)}")
    for note in site.notes:
        console.print(f"  [dim]note: {note}[/dim]")


ENV_FILE = Path(".env")


def save_model_choice(option: ModelOption, env_file: Path = ENV_FILE) -> None:
    """Persist the chosen backend + model to .env (and the current process)."""
    env_file.touch(exist_ok=True)
    key = "INFORMANT_MODEL" if option.backend == "claude" else "OLLAMA_MODEL"
    for k, v in (("INFORMANT_LLM", option.backend), (key, option.model)):
        set_key(str(env_file), k, v, quote_mode="never")
        os.environ[k] = v


@app.command("model")
def model_cmd(
    choice: str | None = typer.Argument(
        None, help="Number from the list, or a model name like claude-haiku-5-5 or gemma4:12b."),
    list_only: bool = typer.Option(False, "--list", "-l", help="Just show the list."),
) -> None:
    """Choose which LLM extracts website data (Claude models or local Ollama models)."""
    load_dotenv()
    options, ollama_up = model_options()
    cur_backend, cur_model = current_model()

    table = Table("#", "Backend", "Model", "Notes", title="LLM models")
    for i, opt in enumerate(options, 1):
        is_current = (opt.backend, opt.model) == (cur_backend, cur_model)
        mark = "[green]* [/green]" if is_current else ""
        table.add_row(str(i), opt.backend, f"{mark}{opt.model}", opt.notes,
                      style="bold" if is_current else None)
    console.print(table)
    if not ollama_up:
        console.print("[dim]Ollama isn't running, so local models aren't listed. "
                      "Start the Ollama app to see them.[/dim]")
    console.print(f"Current: [bold]{cur_backend}/{cur_model}[/bold]")
    if list_only:
        return

    if choice is None:
        choice = typer.prompt("Choose a number (Enter to keep current)", default="", show_default=False)
        if not choice.strip():
            return
    selected: ModelOption | None = None
    if choice.strip().isdigit() and 1 <= int(choice) <= len(options):
        selected = options[int(choice) - 1]
    else:
        selected = next((o for o in options if o.model == choice.strip()), None)
    if selected is None:
        console.print(f"[red]'{choice}' isn't in the list.[/red]")
        raise typer.Exit(1)

    save_model_choice(selected)
    console.print(f"[green]Saved: {selected.backend}/{selected.model} (in .env)[/green]")
    if selected.backend == "claude" and not os.environ.get("ANTHROPIC_API_KEY"):
        console.print("[yellow]Add ANTHROPIC_API_KEY to .env before running a lookup.[/yellow]")


@app.command()
def schema() -> None:
    """Print the JSON Schema of the output format (for downstream programs)."""
    import json

    print(json.dumps(CompanyProfile.model_json_schema(), indent=2))


if __name__ == "__main__":
    app()
