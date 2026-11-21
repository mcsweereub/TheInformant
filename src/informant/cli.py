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
from contextlib import contextmanager
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

from .sources.companies_house import CompaniesHouseClient
from .cache import cache_stats, clear_cache
from .errors import ConfigError, InformantError, NotFoundError
from .llm import STRATEGIES, ModelOption, choose_backend, current_model, model_options
from .pipeline import (
    OPTIONAL_SOURCES,
    REGISTRY_DEPTHS,
    research_company,
    research_customers,
    skipped_sources,
)
from .sources.companies_house import normalise_company_number
from .sources import land_registry
from .progress import ProgressEvent, emit
from .schema import SCHEMA_VERSION, CompanyProfile
from .storage import (
    compare_snapshots,
    default_output_dir,
    load_index,
    load_saved_profile,
    save_profile,
)

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


def _money(value: float | None) -> str:
    if value is None:
        return "n/a"
    sign = "-" if value < 0 else ""
    v = abs(value)
    if v >= 1_000_000:
        return f"{sign}£{v / 1_000_000:.2f}m"
    if v >= 1_000:
        return f"{sign}£{v / 1_000:.0f}k"
    return f"{sign}£{v:.0f}"


def _print_registry(profile: CompanyProfile) -> None:
    fin, own = profile.financials, profile.ownership
    if fin.periods:
        table = Table("Year end", "Turnover", "Profit", "Net assets", "Cash", "Employees",
                      "Read from", title="Filed accounts")
        sources = {"ixbrl": "tagged filing", "annual_report": "annual report", "ocr": "OCR of scan"}
        for p in fin.periods[:4]:
            table.add_row(str(p.period_end), _money(p.turnover), _money(p.profit_loss),
                          _money(p.net_assets), _money(p.cash),
                          str(p.employees) if p.employees is not None else "n/a",
                          sources.get(p.method or "", ""))
        out.print(table)
    for label, value in (("Activities", fin.principal_activities), ("Auditor", fin.auditor),
                         ("Ultimate controlling party", fin.ultimate_controlling_party)):
        if value:
            out.print(f"{label}: {value}", highlight=False)
    for note in fin.notes:
        out.print(f"  [dim]accounts: {note}[/dim]")

    if own.parents:
        chain = " -> ".join(p.name + (" (top of chain)" if p.is_ultimate else "")
                           for p in own.parents)
        out.print(f"Owned by: {chain}")
    if profile.charges.total:
        out.print(f"Charges: {profile.charges.outstanding} outstanding of {profile.charges.total}")
    allotments = sum(1 for e in profile.capital.events if e.kind == "allotment")
    if profile.capital.events:
        out.print(f"Share capital filings: {len(profile.capital.events)} "
                  f"({allotments} allotment{'' if allotments == 1 else 's'})")
    if profile.related_companies.companies:
        names = ", ".join(c.name.title() for c in profile.related_companies.companies[:5])
        more = len(profile.related_companies.companies) - 5
        out.print(f"Related companies ({len(profile.related_companies.companies)}): {names}"
                  + (f" and {more} more" if more > 0 else ""))

    colours = {"high": "red", "warning": "yellow", "info": "cyan"}
    for f in profile.risk.flags:
        evidence = f" [dim]({f.evidence})[/dim]" if f.evidence else ""
        out.print(f"  [{colours[f.severity]}]{f.severity:>7}[/{colours[f.severity]}]  {f.message}{evidence}")


def _print_wikipedia(profile: CompanyProfile) -> None:
    wiki = profile.wikipedia
    if not (wiki.title or wiki.wikidata_id):
        return
    facts = [f"founded {wiki.founded}" if wiki.founded else None,
             ", ".join(wiki.industries) or None,
             f"HQ {wiki.headquarters}" if wiki.headquarters else None,
             f"parent {wiki.parent_organisation}" if wiki.parent_organisation else None,
             "; ".join(wiki.stock_listings) or None]
    how = "matched by company number" if wiki.matched_by == "company_number" else "matched by name"
    out.print(f"Wikipedia: [bold]{wiki.title or wiki.wikidata_id}[/bold] ({how})"
              + (f": {wiki.description}" if wiki.description else ""), highlight=False)
    detail = " | ".join(f for f in facts if f)
    if detail:
        out.print(f"  {detail}", highlight=False)
    if wiki.references:
        out.print(f"  [dim]{len(wiki.references)} references listed in the JSON file[/dim]")


def _print_history_and_funding(profile: CompanyProfile) -> None:
    if profile.history:
        out.print(f"History ({len(profile.history)} milestones):")
        for m in profile.history[:12]:
            out.print(f"  {m.year}  {m.event}", highlight=False)
        if len(profile.history) > 12:
            out.print(f"  [dim]and {len(profile.history) - 12} more in the JSON file[/dim]")
    funding = profile.funding
    for r in funding.rounds:
        parts = [r.year, r.round, r.amount]
        who = f" from {', '.join(r.investors)}" if r.investors else ""
        out.print(f"Funding: {' '.join(p for p in parts if p)}{who}", highlight=False)
    if funding.shareholders:
        holders = ", ".join(s.name + (f" ({s.stake})" if s.stake else "") for s in funding.shareholders)
        out.print(f"Shareholders mentioned: {holders}", highlight=False)
    if profile.key_facts:
        out.print(f"[dim]{len(profile.key_facts)} key facts in the JSON file[/dim]")


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


def _print_public_records(profile: CompanyProfile) -> None:
    lei = profile.lei
    if lei.lei:
        parent = lei.ultimate_parent or lei.direct_parent
        detail = [lei.registration_status or "",
                  f"parent {parent.name} ({parent.country})" if parent else "",
                  f"{lei.subsidiaries_total} subsidiaries with LEIs" if lei.subsidiaries_total else "",
                  f"{len(lei.isins)} securities" if lei.isins else ""]
        out.print(f"LEI: {lei.lei} " + " | ".join(d for d in detail if d), highlight=False)
    pc = profile.public_contracts
    if pc.found:
        sole = f", {_money(pc.sole_supplier_value)} won alone" if pc.sole_supplier_value else ""
        out.print(f"Public contracts: {pc.found} award(s){sole}; buyers: "
                  + ", ".join(list(pc.top_buyers)[:5]), highlight=False)
        for c in pc.items[:5]:
            shared = f" [dim](shared, {c.suppliers} suppliers)[/dim]" if c.suppliers > 1 else ""
            out.print(f"  {str(c.awarded_on or ''):<10}  {c.buyer}: {c.title[:80]} "
                      f"{_money(c.value) if c.value else ''}{shared}", highlight=False)
    pd = profile.political_donations
    if pd.found:
        span = (f", {pd.first_donation:%Y}-{pd.last_donation:%Y}"
                if pd.first_donation and pd.last_donation else "")
        out.print(f"Political donations: {pd.found}, {_money(pd.total_value)} in total{span}")
        for recipient, value in list(pd.by_recipient.items())[:6]:
            out.print(f"  {_money(value):>9}  {recipient}", highlight=False)
    elif pd.group or pd.accounts_statement:
        out.print("Political donations: none by the company itself in the register")
    for g in pd.group:
        top = ", ".join(list(g.by_recipient)[:3])
        out.print(f"  by its {g.relation} {g.name} ({g.company_number}): {g.found} donation(s), "
                  f"{_money(g.total_value)}" + (f"; to {top}" if top else ""), highlight=False)
    if pd.accounts_statement:
        out.print(f"  accounts say: [italic]{pd.accounts_statement}[/italic]", highlight=False)
    for note in pd.notes:
        out.print(f"  [dim]donations: {note}[/dim]")
    _print_property(profile.property)
    h = profile.hiring
    if h.provider:
        depts = ", ".join(f"{d} ({n})" for d, n in list(h.departments.items())[:6])
        if h.total:
            out.print(f"Hiring: {h.total} open role(s) on {h.provider}" + (f": {depts}" if depts else ""),
                      highlight=False)
        else:
            out.print(f"Hiring: no open roles on its {h.provider} board ({h.board_url})", highlight=False)
    site = profile.website
    if site.posts:
        out.print(f"Site posts: {len(site.posts)}, latest: {site.posts[0].title} "
                  f"[dim]({site.posts[0].published_on})[/dim]", highlight=False)
    if site.shop:
        out.print(f"Shop: {site.shop.products_listed} products; types: "
                  + ", ".join(list(site.shop.product_types)[:6]), highlight=False)
    d = profile.domain
    if d.name:
        facts = [f"email {d.email_provider}" if d.email_provider else "",
                 f"archived since {d.first_archived:%Y}" if d.first_archived else "",
                 f"{len(d.subdomains)} subdomains" if d.subdomains else ""]
        out.print(f"Domain {d.name}: " + " | ".join(f for f in facts if f), highlight=False)
        services = list(dict.fromkeys(d.email_services + d.verified_services))
        if services:
            out.print(f"  uses: {', '.join(services)}", highlight=False)
        if d.other_domains:
            out.print(f"  other domains on its certificates: {', '.join(d.other_domains[:8])}",
                      highlight=False)
        for note in d.notes:
            out.print(f"  [dim]domain: {note}[/dim]")


def _print_property(p, limit: int = 6) -> None:
    if p.found:
        paid = (f"; {_money(p.total_price_paid)} paid for the {p.priced_titles} with a recorded price"
                if p.total_price_paid else "")
        regions = ", ".join(f"{r} ({n})" for r, n in list(p.regions.items())[:4])
        out.print(f"Property: {p.found} title(s) in England and Wales ({p.freehold} freehold, "
                  f"{p.leasehold} leasehold){paid}" + (f"; {regions}" if regions else ""),
                  highlight=False)
        for t in p.items[:limit]:
            since = f"{t.owner_since:%Y}" if t.owner_since else "    "
            price = f" {_money(t.price_paid)}" if t.price_paid else ""
            out.print(f"  {since}  {(t.tenure or '')[:9]:<9} {(t.address or t.title_number)[:90]}"
                      f"{price}", highlight=False)
    elif p.group:
        out.print("Property: none registered to the company itself")
    for g in p.group:
        out.print(f"  owned by its {g.relation} {g.name} ({g.company_number}): {g.titles} title(s)"
                  + (f", {_money(g.total_price_paid)} recorded" if g.total_price_paid else ""),
                  highlight=False)
    for note in p.notes:
        out.print(f"  [dim]property: {note}[/dim]")


def _print_customers(profile: CompanyProfile, runs: list[dict] | None = None) -> None:
    if not profile.customers:
        return
    matched = sum(1 for c in profile.customers if c.company_number)
    out.print(f"Customers: {len(profile.customers)} named, {matched} matched on Companies House")
    for c in profile.customers[:15]:
        reg = (f" -> {c.registered_name} ({c.company_number}, {c.status}"
               + (", likely match" if c.match == "likely" else "") + ")") if c.company_number else ""
        done = " [green]researched[/green]" if c.researched else ""
        evidence = f" [dim]{c.evidence}[/dim]" if c.evidence else ""
        out.print(f"  {c.name}{reg}{evidence}{done}", highlight=False)
    if len(profile.customers) > 15:
        out.print(f"  [dim]and {len(profile.customers) - 15} more in the JSON file[/dim]")
    for run in runs or []:
        if not run["ok"]:
            out.print(f"  [red]could not research {run['name']}: {run['error']}[/red]")
    if matched and not any(c.researched for c in profile.customers):
        out.print(f"  [dim]research them later with: informant customers "
                  f"{profile.identity.company_number} --research[/dim]")


def _print_mentions(profile: CompanyProfile) -> None:
    wm = profile.web_mentions
    if not (wm.items or wm.notes):
        return
    out.print(f"Web mentions: {len(wm.items)} (web searches used this lookup: {wm.searches_used})")
    for m in wm.items[:5]:
        out.print(f"  {m.title} [dim]({m.url})[/dim]", highlight=False)
    for note in wm.notes:
        out.print(f"  [dim]search: {note}[/dim]")


def _print_news(profile: CompanyProfile) -> None:
    news = profile.news
    if not news.items and not news.notes:
        return
    marks = {-1.0: "[red]-[/red]", 0.0: "[dim]o[/dim]", 1.0: "[green]+[/green]"}
    shown = [n for n in news.items if n.relevant is not False]
    out.print(f"News: {len(shown)} article(s) in the last {news.window_days} days"
              + (f" (searched for: {', '.join(news.searched_for)})" if news.searched_for else ""))
    for item in shown[:8]:
        when = item.published_at.date().isoformat() if item.published_at else "          "
        mark = marks.get(item.sentiment, " ")
        topic = f" [dim]{item.topic}[/dim]" if item.topic else ""
        out.print(f"  {mark} {when}  {item.title} [dim]({item.publisher or item.provider})[/dim]{topic}",
                  highlight=False)
    if len(shown) > 8:
        out.print(f"  [dim]and {len(shown) - 8} more in the JSON file[/dim]")
    for note in news.notes:
        out.print(f"  [dim]news: {note}[/dim]")


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


def _print_changes(comparison, limit: int | None = None) -> None:
    if comparison is None:
        return
    when = f"{comparison.previous_at:%Y-%m-%d %H:%M}"
    if not comparison.changes:
        out.print(f"No changes since the lookup on {when}.")
    else:
        out.print(f"Changes since the lookup on {when}: {len(comparison.changes)}")
        marks = {"added": "[green]+[/green]", "removed": "[red]-[/red]", "changed": "[yellow]~[/yellow]"}
        shown = comparison.changes[:limit] if limit else comparison.changes
        for c in shown:
            out.print(f"  {marks[c.kind]} [dim]{c.section:<19}[/dim] {c.summary}", highlight=False)
        if len(shown) < len(comparison.changes):
            out.print(f"  [dim]and {len(comparison.changes) - len(shown)} more: informant changes "
                      f"{comparison.company_number}[/dim]")
    if comparison.not_compared:
        out.print(f"  [dim]not compared (only one of the two lookups checked them): "
                  f"{', '.join(comparison.not_compared)}[/dim]")


# --- changes ------------------------------------------------------------------


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
        from datetime import datetime, timezone

        number = normalise_company_number(company)
        if number is None:
            raise ConfigError(f"'{company}' isn't a company number.")
        when = None
        if since:
            try:
                when = datetime.strptime(since, "%Y-%m-%d").replace(hour=23, minute=59, second=59,
                                                                    tzinfo=timezone.utc)
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


# --- customers ----------------------------------------------------------------


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


@app.command("graph")
def graph_cmd(
    company: str | None = typer.Argument(
        None, help="Company number or exact name: show its links and paths to other saved companies."),
    hops: int = typer.Option(2, "--hops", min=1, max=4,
                             help="With a company: how many steps to search for other saved companies."),
    out_dir: Path | None = typer.Option(
        None, "--out", "-o", help="Profiles folder [default: INFORMANT_OUTPUT_DIR or ./output]."),
    json_mode: bool = JSON_OPT,
) -> None:
    """Build graph.json (every link between companies and organisations in the folder)."""
    from . import graph as g

    def body() -> None:
        folder = (out_dir or default_output_dir()).resolve()
        built, path = g.save_graph(folder)
        summary = g.stats(built)
        if company is None:
            if json_mode:
                print_json({"ok": True, "command": "graph", "path": str(path), **summary})
                return
            out.print(f"Graph: {summary['nodes']} organisations, {summary['edges']} links, from "
                      f"{summary['companies_saved']} saved profile(s) -> {path}")
            for kind, n in sorted(summary["edge_types"].items(), key=lambda x: -x[1]):
                out.print(f"  {n:>5}  {kind.replace('_', ' ')}")
            return
        node = g.find_node(built, company)
        if node is None:
            raise NotFoundError(f"{company!r} is not in the graph of {folder}")
        links = g.neighbours(built, node.id)
        paths = [c for c in g.connections(built, node.id, max_hops=hops)]
        if json_mode:
            print_json({"ok": True, "command": "graph", "path": str(path), "node": node.model_dump(),
                        "links": [{"other": x["other"].model_dump(), "outgoing": x["outgoing"],
                                   "edge": x["edge"].model_dump(mode="json")} for x in links],
                        "connections": [{"company": c["company"].model_dump(),
                                         "path": [{"edge": e.model_dump(mode="json"), "to": n.model_dump()}
                                                  for e, n in c["path"]]} for c in paths]})
            return
        out.print(f"[bold]{node.name}[/bold] ({node.company_number or node.kind}): {len(links)} link(s)",
                  highlight=False)
        current = None
        for x in links:
            e = x["edge"]
            if e.type != current:
                current = e.type
                out.print(f"  [bold]{e.type.replace('_', ' ')}[/bold]")
            arrow = "->" if not e.directed else ("->" if x["outgoing"] else "<-")
            other = x["other"]
            tag = " [green](saved)[/green]" if other.saved else ""
            reason = f" [dim]{e.evidence[0]}[/dim]" if e.evidence else ""
            out.print(f"    {arrow} {other.name}{f' ({other.company_number})' if other.company_number else ''}"
                      f"{tag}{reason}", highlight=False)
        if paths:
            out.print(f"[bold]Connected saved companies (within {hops} steps)[/bold]")
            for c in paths:
                steps = "; ".join(g.describe_edge(e, built) for e, _ in c["path"])
                out.print(f"  {c['company'].name}: {steps}", highlight=False)
        else:
            out.print(f"[dim]No other saved company within {hops} steps.[/dim]")

    _run("graph", json_mode, body)


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


# --- cache --------------------------------------------------------------------


@app.command("cache")
def cache_cmd(
    clear: bool = typer.Option(False, "--clear", help="Delete cached entries."),
    pages_only: bool = typer.Option(False, "--pages", help="With --clear: only crawled pages."),
    llm_only: bool = typer.Option(False, "--llm", help="With --clear: only LLM answers."),
    json_mode: bool = JSON_OPT,
) -> None:
    """Show (or clear) the local cache of crawled pages and LLM answers."""

    def body() -> None:
        removed = None
        if clear:
            both = not (pages_only or llm_only)
            removed = clear_cache(pages=both or pages_only, llm=both or llm_only)
        stats = cache_stats()
        if json_mode:
            print_json({"ok": True, "command": "cache", "removed": removed, **stats})
            return
        if removed is not None:
            out.print(f"[green]Removed {removed} cached file(s).[/green]")
        state = "on" if stats["enabled"] else "off (INFORMANT_CACHE)"
        out.print(f"Cache: {stats['path']}  [{state}]")
        for name, label in (("pages", "crawled pages"), ("llm", "LLM answers"),
                            ("accounts", "accounts documents")):
            s = stats[name]
            out.print(f"  {label}: {s['entries']} ({s['bytes'] / 1_048_576:.1f} MB)")

    _run("cache", json_mode, body)


@app.command("property")
def property_cmd(
    company: str | None = typer.Argument(None, help="Company number to show (local data only)."),
    do_update: bool = typer.Option(False, "--update",
                                   help="Download the dataset (first time) or apply new monthly changes."),
    full: bool = typer.Option(False, "--full", help="With --update: rebuild from the latest full file."),
    json_mode: bool = JSON_OPT,
) -> None:
    """HM Land Registry's companies-that-own-property dataset: status, update, or one company."""

    def body() -> None:
        store = land_registry.PropertyStore()
        result = None
        if do_update or full:
            notify = None if json_mode else (lambda m: err.print(f"[dim]{m}[/dim]"))
            result = land_registry.update(store, full=full, notify=notify)
        if company:
            number = normalise_company_number(company)
            if not store.exists():
                raise NotFoundError("no local Land Registry data: run `informant property --update`")
            holdings = land_registry.fetch_property(number, store=store)
            if json_mode:
                print_json({"ok": True, "command": "property", "company_number": number,
                            "update": result, "property": holdings.model_dump(mode="json")})
                return
            if not holdings.found:
                out.print(f"No titles registered to company number {number}.")
            _print_property(holdings, limit=40)
            return
        meta = store.meta()
        status = {"path": str(store.path), "exists": store.exists(),
                  "dataset": meta.get("dataset") or None, "month": meta.get("month") or None,
                  "published": meta.get("published") or None,
                  "titles": int(meta["titles"]) if meta.get("titles") else None,
                  "changes_applied": [c for c in (meta.get("changes") or "").split(",") if c],
                  "updated_at": meta.get("updated_at") or None,
                  "bytes": store.path.stat().st_size if store.exists() else 0,
                  "api_key_set": land_registry.api_key() is not None}
        if json_mode:
            print_json({"ok": True, "command": "property", "update": result, **status})
            return
        if result:
            what = {"none": "already up to date", "changes": "applied " + ", ".join(result["files"]),
                    "full": "built from " + ", ".join(result["files"])}[result["action"]]
            out.print(f"[green]Land Registry data {what}.[/green]")
        if not status["exists"]:
            out.print("No local Land Registry data. Run `informant property --update` "
                      "(downloads the full monthly file once, then small monthly changes).")
            if not status["api_key_set"]:
                out.print("[yellow]LAND_REGISTRY_API_KEY is not set.[/yellow]")
            return
        out.print(f"Land Registry data: {status['titles']:,} titles, month {status['month']} "
                  f"(published {status['published']}), {status['bytes'] / 1_073_741_824:.2f} GB")
        out.print(f"  {status['path']}  [dim]built from {meta.get('base')}"
                  + (f", then {len(status['changes_applied'])} change file(s)" if status["changes_applied"] else "")
                  + "[/dim]")

    _run("property", json_mode, body)


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
