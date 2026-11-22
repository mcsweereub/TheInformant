"""Commands that research companies: search, lookup, changes, customers."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC
from pathlib import Path

import typer
from rich.table import Table

from ..errors import ConfigError, NotFoundError
from ..llm import STRATEGIES
from ..pipeline import (
    OPTIONAL_SOURCES,
    REGISTRY_DEPTHS,
    research_company,
    research_customers,
    skipped_sources,
)
from ..progress import emit
from ..schema import CompanyProfile
from ..sources.companies_house import normalise_company_number
from ..storage import (
    compare_snapshots,
    default_output_dir,
    load_saved_profile,
    save_profile,
)
from .common import (
    JSON_OPT,
    _ch_client,
    _progress,
    _run,
    app,
    err,
    out,
    print_json,
)
from .render import (
    _print_changes,
    _print_customers,
    _print_history_and_funding,
    _print_mentions,
    _print_news,
    _print_public_records,
    _print_registry,
    _print_website,
    _print_wikipedia,
)


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
    llm_strategy: str | None = typer.Option(
        None, "--llm-strategy", help="auto, single (one LLM call) or staged (notes per part, then "
        "a merge; for local models) [default: INFORMANT_LLM_STRATEGY or auto]."),
    refresh: bool = typer.Option(
        False, "--refresh", help="Re-download pages even if cached (unchanged pages still reuse "
        "cached LLM answers)."),
    cache: bool = typer.Option(
        True, "--cache/--no-cache", help="Use the local cache of pages and LLM answers."),
    registry: str | None = typer.Option(
        None, "--registry", help="full (also filed accounts, share capital, charges, ownership, "
        "related companies) or basic [default: INFORMANT_REGISTRY or full]."),
    news: bool | None = typer.Option(
        None, "--news/--no-news", help="Search recent news [default: INFORMANT_NEWS, on]."),
    news_days: int | None = typer.Option(
        None, "--news-days", help="How far back to look for news [default: 90]."),
    wikipedia: bool | None = typer.Option(
        None, "--wikipedia/--no-wikipedia",
        help="Look up Wikipedia/Wikidata (needs INFORMANT_CONTACT) [default: INFORMANT_WIKIPEDIA, on]."),
    pdf_accounts: bool | None = typer.Option(
        None, "--pdf-accounts/--no-pdf-accounts",
        help="When accounts are filed as PDF only, read figures from the company's annual report or "
        "an OCR of the scan (needs an LLM) [default: INFORMANT_PDF_ACCOUNTS, on]."),
    web_search: bool | None = typer.Option(
        None, "--search/--no-search",
        help="Use web search (Tavily, needs TAVILY_API_KEY) for mentions and to find a missing "
        "website or annual report [default: INFORMANT_SEARCH, on]."),
    skip: list[str] | None = typer.Option(
        None, "--skip", help="Public-data sources to leave out, comma-separated: "
        + ", ".join(OPTIONAL_SOURCES) + " or all [default: INFORMANT_SKIP]."),
    with_customers: int = typer.Option(
        0, "--with-customers", min=0,
        help="Also research up to this many of the company's matched UK customers, each saved as "
        "its own profile (or later: informant customers <number> --research)."),
    json_mode: bool = JSON_OPT,
    progress: bool = typer.Option(False, "--progress", help="Write JSON progress events to stderr."),
) -> None:
    """Build a CompanyProfile JSON for one company and save it."""

    def body() -> None:
        if out_dir is not None and out_file is not None:
            raise ConfigError("Use --out or --out-file, not both.")
        if browser is not None and browser.lower() not in ("auto", "always", "never"):
            raise ConfigError("--browser must be auto, always or never.")
        if llm_strategy is not None and llm_strategy.lower() not in STRATEGIES:
            raise ConfigError("--llm-strategy must be auto, single or staged.")
        if registry is not None and registry.lower() not in REGISTRY_DEPTHS:
            raise ConfigError("--registry must be full or basic.")
        if skip:
            skipped_sources(skip)  # check the names before starting
        kwargs = dict(website=website, web=web, llm=llm, max_pages=max_pages, filings=filings,
                      browser=browser, llm_strategy=llm_strategy, use_cache=cache,
                      refresh=refresh, registry=registry, news=news,
                      news_days=news_days, wikipedia=wikipedia,
                      pdf_accounts=pdf_accounts, web_search=web_search, skip=skip or None)

        with _progress(progress, json_mode) as callback:
            result = research_company(query, progress=callback, **kwargs)
            emit(callback, "save", "started", "Saving profile")
            path, index_path = save_profile(result.profile, out_dir=out_dir, out_file=out_file)
            emit(callback, "save", "done", str(path), path=str(path))
            number = result.profile.identity.company_number
            comparison = compare_snapshots(path.parent, number) if number else None
            runs = []
            if with_customers:
                runs = _research_and_save_customers(result.profile, path, with_customers, kwargs,
                                                    callback)

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
                "customer_research": runs,
                "changes": comparison.model_dump(mode="json") if comparison else None,
                "profile": profile.model_dump(mode="json"),
            })
            return

        if result.matched_by == "search":
            err.print(f"Using top match: [bold]{profile.identity.name}[/bold] "
                      f"({profile.identity.company_number})")
            for h in result.alternatives:
                err.print(f"  [dim]also: {h.title} ({h.company_number}, {h.status})[/dim]")
        _print_registry(profile)
        _print_wikipedia(profile)
        if web:
            _print_website(profile)
        elif profile.identity.description:
            out.print(f"  [italic]{profile.identity.description}[/italic]", highlight=False)
        _print_history_and_funding(profile)
        _print_public_records(profile)
        _print_news(profile)
        _print_mentions(profile)
        _print_customers(profile, runs)
        _print_changes(comparison, limit=12)
        active = sum(1 for o in profile.people.officers if o.active)
        out.print(
            f"[green]Wrote {path}[/green]  "
            f"status={profile.identity.status}, active officers={active}, "
            f"PSCs={len(profile.people.significant_control)}, filings={len(profile.filings.recent)}"
        )

    _run("lookup", json_mode, body)


def _research_and_save_customers(profile: CompanyProfile, main_path: Path, limit: int,
                                 options: dict, callback) -> list[dict]:
    """Research up to `limit` matched customers with the same options, save each next to
    the main profile, mark them on the main profile and save it again."""
    options = {k: v for k, v in options.items() if k != "website"}  # that was the main company's
    runs = research_customers(profile, limit=limit, progress=callback, **options)
    summary = []
    for run in runs:
        entry = {"name": run.name, "company_number": run.company_number, "ok": run.error is None,
                 "path": None, "error": run.error}
        if run.result is not None:
            path, _ = save_profile(run.result.profile, out_dir=main_path.parent)
            entry["path"] = str(path)
            for c in profile.customers:
                if c.company_number == run.company_number:
                    c.researched, c.profile_path = True, str(path)
        summary.append(entry)
    if runs:
        save_profile(profile, out_file=main_path)  # record which customers were researched
    return summary


@app.command("changes")
def changes_cmd(
    company: str = typer.Argument(..., help="Company number of a profile you've saved at least twice."),
    since: str | None = typer.Option(
        None, "--since", help="Compare with the last lookup on or before this date (YYYY-MM-DD) "
        "instead of the one before the latest."),
    out_dir: Path | None = typer.Option(
        None, "--out", "-o", help="Folder holding the saved profile [default: INFORMANT_OUTPUT_DIR or ./output]."),
    json_mode: bool = JSON_OPT,
) -> None:
    """Show what changed between the latest lookup of a company and an earlier one."""

    def body() -> None:
        from datetime import datetime

        number = normalise_company_number(company)
        if number is None:
            raise ConfigError(f"'{company}' isn't a company number.")
        when = None
        if since:
            try:
                when = datetime.strptime(since, "%Y-%m-%d").replace(hour=23, minute=59, second=59,
                                                                    tzinfo=UTC)
            except ValueError:
                raise ConfigError("--since must be a date like 2026-11-01.") from None
        folder = (out_dir or default_output_dir()).resolve()
        comparison = compare_snapshots(folder, number, since=when)
        if comparison is None:
            raise NotFoundError(f"Fewer than two saved lookups of {number} in {folder / 'history'}; "
                                f"run `informant lookup {number}` again later to compare.")
        if json_mode:
            print_json({"ok": True, "command": "changes", "company_number": number,
                        **comparison.model_dump(mode="json")})
            return
        out.print(f"[bold]{comparison.name}[/bold] ({number})")
        _print_changes(comparison)

    _run("changes", json_mode, body)


@app.command("customers")
def customers_cmd(
    company: str = typer.Argument(..., help="Company number of a profile you've already saved."),
    research: bool = typer.Option(False, "--research", help="Research the matched customers now."),
    limit: int = typer.Option(5, "--limit", "-n", min=1, help="Most customers to research."),
    out_dir: Path | None = typer.Option(
        None, "--out", "-o", help="Folder holding the saved profile [default: INFORMANT_OUTPUT_DIR or ./output]."),
    llm: bool = typer.Option(True, "--llm/--no-llm", help="Use an LLM for each customer."),
    web: bool = typer.Option(True, "--web/--no-web", help="Crawl each customer's website."),
    news: bool | None = typer.Option(None, "--news/--no-news", help="Search news for each customer."),
    json_mode: bool = JSON_OPT,
    progress: bool = typer.Option(False, "--progress", help="Write JSON progress events to stderr."),
) -> None:
    """List a saved company's customers, and optionally research them (each saved as its own profile)."""

    def body() -> None:
        number = normalise_company_number(company)
        if number is None:
            raise ConfigError(f"'{company}' isn't a company number.")
        folder = (out_dir or default_output_dir()).resolve()
        saved = load_saved_profile(folder, number)
        if saved is None:
            raise NotFoundError(f"No saved profile for {number} in {folder}; run `informant lookup {number}` first.")
        profile, path = saved

        runs: list[dict] = []
        if research:
            with _progress(progress, json_mode) as callback:
                runs = _research_and_save_customers(
                    profile, path, limit, dict(llm=llm, web=web, news=news), callback)
        if json_mode:
            print_json({"ok": True, "command": "customers", "company_number": number,
                        "name": profile.identity.name, "path": str(path),
                        "customers": [c.model_dump(mode="json") for c in profile.customers],
                        "customer_research": runs})
            return
        if not profile.customers:
            out.print(f"No customers recorded for {profile.identity.name}.")
            return
        _print_customers(profile, runs)

    _run("customers", json_mode, body)
