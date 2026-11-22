"""Readable (non-JSON) output of a profile's sections, for people."""

from __future__ import annotations

from rich.table import Table

from ..schema import CompanyProfile
from .common import (
    out,
)


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
        where = ", ".join(x for x in (g.company_number, (g.country or "").title()) if x)
        out.print(f"  owned by its {g.relation} {g.name}" + (f" ({where})" if where else "")
                  + f": {g.titles} title(s)"
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
