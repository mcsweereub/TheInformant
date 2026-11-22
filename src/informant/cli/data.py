"""Commands over saved and downloaded data: list, graph, cache, property."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from ..cache import cache_stats, clear_cache
from ..errors import ConfigError, NotFoundError
from ..sources import land_registry
from ..sources.companies_house import normalise_company_number
from ..storage import (
    default_output_dir,
    load_index,
)
from .common import (
    JSON_OPT,
    _run,
    app,
    err,
    out,
    print_json,
)
from .render import (
    _print_property,
)


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
    from .. import graph as g

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
    company: str | None = typer.Argument(
        None, help="Company number to show, or an overseas company's exact name (local data only)."),
    do_update: bool = typer.Option(False, "--update",
                                   help="Download the datasets (first time) or apply new monthly changes."),
    full: bool = typer.Option(False, "--full", help="With --update: rebuild from the latest full file."),
    dataset: str = typer.Option("all", "--dataset",
                                help="ccod (UK companies), ocod (overseas companies) or all."),
    json_mode: bool = JSON_OPT,
) -> None:
    """HM Land Registry's UK and overseas companies-that-own-property datasets: status,
    update, or one company.

    Without arguments: what the local copies hold. --update downloads them the first time,
    then applies each month's change files."""

    def body() -> None:
        if dataset not in (*land_registry.DATASETS, "all"):
            raise ConfigError("--dataset must be ccod, ocod or all")
        chosen = land_registry.DATASETS if dataset == "all" else (dataset,)
        stores = {ds: land_registry.PropertyStore(dataset=ds) for ds in land_registry.DATASETS}
        results: dict[str, dict] = {}
        if do_update or full:
            notify = None if json_mode else (lambda m: err.print(f"[dim]{m}[/dim]"))
            for ds in chosen:
                results[ds] = land_registry.update(stores[ds], full=full, notify=notify)
        if company:
            number = normalise_company_number(company)
            ds = (land_registry.OVERSEAS if number is None or land_registry.is_overseas_number(number)
                  else land_registry.UK)
            store = stores[ds]
            if not store.exists():
                raise NotFoundError(f"no local copy of the {land_registry.TITLES[ds]} dataset: run "
                                    f"`informant property --update --dataset {ds}`")
            holdings = land_registry.fetch_property(number, [] if number else [company], store=store)
            if json_mode:
                print_json({"ok": True, "command": "property", "company_number": number,
                            "dataset": ds, "update": results.get(land_registry.UK),
                            "updates": results, "property": holdings.model_dump(mode="json")})
                return
            if not holdings.found:
                out.print(f"No titles registered to {number or company}.")
            _print_property(holdings, limit=40)
            return
        statuses = {ds: _property_status(stores[ds]) for ds in land_registry.DATASETS}
        if json_mode:
            # top level: the UK dataset, as before the overseas one was added
            print_json({"ok": True, "command": "property", "update": results.get(land_registry.UK),
                        "updates": results, **statuses[land_registry.UK], "datasets": statuses,
                        "api_key_set": land_registry.api_key() is not None})
            return
        for ds in land_registry.DATASETS:
            status, result = statuses[ds], results.get(ds)
            label = land_registry.TITLES[ds]
            if result:
                what = {"none": "already up to date", "changes": "applied " + ", ".join(result["files"]),
                        "full": "built from " + ", ".join(result["files"])}[result["action"]]
                out.print(f"[green]{label}: {what}.[/green]")
            if not status["exists"]:
                out.print(f"{label}: no local copy. Run `informant property --update` "
                          "(downloads the full monthly file once, then small monthly changes).")
                continue
            size = status["bytes"] / 1_048_576
            out.print(f"{label}: {status['titles']:,} titles, month {status['month']} "
                      f"(published {status['published']}), "
                      + (f"{size / 1024:.2f} GB" if size >= 1024 else f"{size:.0f} MB"))
            out.print(f"  {status['path']}  [dim]built from {status['base']}"
                      + (f", then {len(status['changes_applied'])} change file(s)"
                         if status["changes_applied"] else "") + "[/dim]")
        if land_registry.api_key() is None:
            out.print("[yellow]LAND_REGISTRY_API_KEY is not set.[/yellow]")

    _run("property", json_mode, body)


def _property_status(store) -> dict:
    meta = store.meta()
    return {"path": str(store.path), "exists": store.exists(),
            "dataset": meta.get("dataset") or None, "month": meta.get("month") or None,
            "published": meta.get("published") or None, "base": meta.get("base") or None,
            "titles": int(meta["titles"]) if meta.get("titles") else None,
            "changes_applied": [c for c in (meta.get("changes") or "").split(",") if c],
            "updated_at": meta.get("updated_at") or None,
            "bytes": store.path.stat().st_size if store.exists() else 0}
