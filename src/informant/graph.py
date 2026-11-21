"""The relationship graph: every company-to-organisation link in a folder of saved profiles,
as one set of nodes and edges (for the GUI, or for asking "how are these two connected?").

Only organisations are nodes: companies, public bodies, political parties, funds. People
never are. Shared officers become an edge between the two companies (the officers'
names, from Companies House, are the evidence), and donations to individual politicians
are left out.

Edges (source -> target):
    owns                   parent -> company (PSC chain, LEI direct parent, LEI subsidiaries)
    ultimate_parent_of     LEI ultimate parent -> company (when not also the direct parent)
    supplies               company -> customer (named customers; public-sector buyers)
    invests_in             investor or shareholder -> company (funding rounds, shareholders)
    donated_to             company -> party or campaign group (Electoral Commission)
    shares_officers        company -- company (undirected; current officers in common)
    co_owns_property_with  company -- organisation (undirected; on the same Land Registry title)
    shares_analytics_with  company -- company (undirected; same analytics or tag ID)

Nodes known by company number get the id "ch:<number>"; others are matched by exact legal
name (`legal_key`) to a numbered node when one exists, else get "org:<legal name>" (or
"body:" for public buyers, "party:" for parties). Saved companies have `saved: true`.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .schema import CompanyProfile, utcnow
from .sources.companies_house.client import normalise_company_number
from .sources.website.extract import legal_key
from .storage import load_index, load_profile_file, write_atomic

GRAPH_VERSION = 1
GRAPH_NAME = "graph.json"
UNDIRECTED = {"shares_officers", "co_owns_property_with", "shares_analytics_with"}
MAX_EVIDENCE = 5

NodeKind = Literal["company", "organisation", "public_body", "party"]


class Node(BaseModel):
    id: str
    kind: NodeKind = "organisation"
    name: str
    company_number: str | None = None
    lei: str | None = None
    country: str | None = None
    status: str | None = None
    saved: bool = False  # a profile of it is in the folder
    file: str | None = None  # that profile's file name


class Edge(BaseModel):
    source: str
    target: str
    type: str
    directed: bool = True
    evidence: list[str] = Field(default_factory=list)  # short, human-readable reasons
    reported_by: list[str] = Field(default_factory=list)  # ids of the saved profiles it came from
    data: list[str] = Field(default_factory=list)  # the sources: companies_house, gleif...
    count: int | None = None  # e.g. contract awards, donations, shared titles
    value: float | None = None  # GBP where it means something (donations, sole-supplier awards)


class Graph(BaseModel):
    graph_version: int = GRAPH_VERSION
    generated_at: datetime = Field(default_factory=utcnow)
    companies_saved: int = 0
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)

    def node(self, node_id: str) -> Node | None:
        return next((n for n in self.nodes if n.id == node_id), None)


def _number(value: str | None) -> str | None:
    if not value:
        return None
    return normalise_company_number(value)


class _Builder:
    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.by_name: dict[str, str] = {}  # legal_key -> node id (numbered nodes win)
        self.edges: dict[tuple[str, str, str], Edge] = {}
        self.pending: list[tuple] = []  # edges whose name-only ends resolve at the end

    # --- nodes ---------------------------------------------------------------------------

    def company(self, name: str, number: str | None = None, *, lei: str | None = None,
                country: str | None = None, status: str | None = None,
                kind: NodeKind = "company") -> str:
        """The id of a node, creating or enriching it."""
        num = _number(number)
        key = legal_key(name or "")
        if num:
            node_id = f"ch:{num}"
        elif lei and f"lei:{lei}" in self.nodes:
            node_id = f"lei:{lei}"
        else:
            prefix = {"public_body": "body", "party": "party"}.get(kind, "org")
            node_id = f"{prefix}:{key or (name or '').strip().lower()}"
        node = self.nodes.get(node_id)
        if node is None:
            node = self.nodes[node_id] = Node(id=node_id, kind=kind if not num else "company",
                                              name=(name or "").strip() or node_id,
                                              company_number=num)
        node.lei = node.lei or lei
        node.country = node.country or country
        node.status = node.status or status
        if key and (num or key not in self.by_name):
            self.by_name[key] = node_id
        return node_id

    # --- edges ---------------------------------------------------------------------------

    def link(self, source: str, target: str, type_: str, *, reporter: str, data: str,
             evidence: str | None = None, count: int | None = None, value: float | None = None) -> None:
        if source == target:
            return
        directed = type_ not in UNDIRECTED
        a, b = (source, target) if directed else tuple(sorted((source, target)))
        edge = self.edges.get((a, b, type_))
        if edge is None:
            edge = self.edges[(a, b, type_)] = Edge(source=a, target=b, type=type_, directed=directed)
        if evidence and evidence not in edge.evidence and len(edge.evidence) < MAX_EVIDENCE:
            edge.evidence.append(evidence)
        if reporter not in edge.reported_by:
            edge.reported_by.append(reporter)
        if data not in edge.data:
            edge.data.append(data)
        if count is not None:
            edge.count = max(edge.count or 0, count)
        if value is not None:
            edge.value = max(edge.value or 0, value)

    def resolve(self) -> None:
        """Merge name-only nodes into numbered nodes with exactly the same legal name."""
        alias: dict[str, str] = {}
        for node_id, node in self.nodes.items():
            if node.company_number:
                continue
            target = self.by_name.get(legal_key(node.name))
            if target and target != node_id and self.nodes[target].company_number:
                alias[node_id] = target
                keep = self.nodes[target]
                keep.lei = keep.lei or node.lei
                keep.country = keep.country or node.country
        if not alias:
            return
        for node_id in alias:
            del self.nodes[node_id]
        old = list(self.edges.values())
        self.edges = {}
        for e in old:
            s, t = alias.get(e.source, e.source), alias.get(e.target, e.target)
            if s == t:
                continue
            if not e.directed:
                s, t = sorted((s, t))
            merged = self.edges.get((s, t, e.type))
            if merged is None:
                self.edges[(s, t, e.type)] = e.model_copy(update={"source": s, "target": t})
                continue
            for field in ("evidence", "reported_by", "data"):
                items = getattr(merged, field)
                items.extend(x for x in getattr(e, field) if x not in items)
            del merged.evidence[MAX_EVIDENCE:]
            if e.count is not None:
                merged.count = max(merged.count or 0, e.count)
            if e.value is not None:
                merged.value = max(merged.value or 0, e.value)


def add_profile(b: _Builder, p: CompanyProfile, file_name: str | None = None) -> str:
    """Add one saved profile's links; returns its node id."""
    ident = p.identity
    me = b.company(ident.name, ident.company_number, lei=p.lei.lei, country="GB", status=ident.status)
    node = b.nodes[me]
    node.saved, node.file = True, file_name

    # ownership: the PSC chain (each parent controls the company one level down)
    chain = sorted(p.ownership.parents, key=lambda x: x.level)
    below = {0: me}
    for parent in chain:
        pid = b.company(parent.name, parent.company_number, country=parent.country_registered,
                        status=parent.status)
        child = below.get(parent.level - 1, me)
        if parent.controls and legal_key(parent.controls) != legal_key(b.nodes[child].name):
            child = b.company(parent.controls)
        b.link(pid, child, "owns", reporter=me, data="companies_house",
               evidence="person with significant control"
               + (f" ({', '.join(parent.natures_of_control[:2])})" if parent.natures_of_control else ""))
        below[parent.level] = pid
    if not chain:  # basic registry depth: corporate PSCs only, by name
        for psc in p.people.significant_control:
            if psc.ceased_on is None and "corporate" in (psc.kind or ""):
                b.link(b.company(psc.name), me, "owns", reporter=me, data="companies_house",
                       evidence="corporate person with significant control")

    # LEI record: parents and subsidiaries as GLEIF reports them
    lei = p.lei
    if lei.direct_parent:
        dp = lei.direct_parent
        b.link(b.company(dp.name, dp.company_number, lei=dp.lei, country=dp.country), me, "owns",
               reporter=me, data="gleif", evidence="direct parent in the LEI record")
    if lei.ultimate_parent and (not lei.direct_parent or lei.ultimate_parent.lei != lei.direct_parent.lei):
        up = lei.ultimate_parent
        b.link(b.company(up.name, up.company_number, lei=up.lei, country=up.country), me,
               "ultimate_parent_of", reporter=me, data="gleif", evidence="ultimate parent in the LEI record")
    for child in lei.subsidiaries:
        b.link(me, b.company(child.name, child.company_number, lei=child.lei, country=child.country),
               "owns", reporter=me, data="gleif", evidence="subsidiary in the LEI record")

    # current officers in common
    for rel in p.related_companies.companies:
        names = ", ".join(rel.shared_officers[:3])
        b.link(me, b.company(rel.name, rel.company_number, status=rel.status), "shares_officers",
               reporter=me, data="companies_house", count=len(rel.shared_officers) or None,
               evidence=f"shared officer(s): {names}" if names else None)

    # customers named on the site / in sources, and public-sector buyers
    for c in p.customers:
        number = c.company_number if c.match in ("exact", "likely") else None
        b.link(me, b.company(c.registered_name or c.name, number, status=c.status), "supplies",
               reporter=me, data="llm_extraction",
               evidence=(c.evidence or "named as a customer")
               + (" (register match: likely)" if c.match == "likely" else ""))
    buyers: dict[str, list] = {}
    for award in p.public_contracts.items:
        if award.buyer:
            buyers.setdefault(award.buyer, []).append(award)
    for buyer, awards in buyers.items():
        sole = sum(a.value or 0 for a in awards if a.suppliers <= 1)
        b.link(me, b.company(buyer, kind="public_body"), "supplies", reporter=me,
               data="contracts_finder", count=len(awards), value=sole or None,
               evidence=f"public contract: {awards[0].title[:80]}")

    # funding and shareholders (organisations only)
    for rnd in p.funding.rounds:
        for investor in rnd.investors:
            b.link(b.company(investor), me, "invests_in", reporter=me, data="llm_extraction",
                   evidence=" ".join(x for x in (rnd.year, rnd.round, rnd.amount) if x) or "funding round")
    for holder in p.funding.shareholders:
        b.link(b.company(holder.name), me, "invests_in", reporter=me, data="llm_extraction",
               evidence="shareholder" + (f" ({holder.stake})" if holder.stake else ""))

    # political donations to parties and campaign groups (never to individuals here)
    def donations(source_id: str, items) -> None:
        by_recipient: dict[str, list] = {}
        for d in items:
            if not d.to_individual:
                by_recipient.setdefault(d.recipient, []).append(d)
        for recipient, ds in by_recipient.items():
            kind: NodeKind = "party" if "party" in (ds[0].recipient_type or "").lower() else "organisation"
            years = sorted({d.accepted_on.year for d in ds if d.accepted_on})
            span = f", {years[0]}-{years[-1]}" if len(years) > 1 else (f", {years[0]}" if years else "")
            b.link(source_id, b.company(recipient, kind=kind), "donated_to", reporter=me,
                   data="electoral_commission", count=len(ds),
                   value=round(sum(d.value for d in ds), 2), evidence=f"{len(ds)} donation(s){span}")

    donations(me, p.political_donations.items)
    for g in p.political_donations.group:
        gid = b.company(g.name, g.company_number)
        donations(gid, g.items)

    # organisations on the same Land Registry titles
    shared: dict[tuple[str, str | None], int] = {}
    for title in p.property.items:
        for co in title.co_owners:
            shared[(co.name, co.company_number)] = shared.get((co.name, co.company_number), 0) + 1
    for (name, number), n in shared.items():
        b.link(me, b.company(name, number), "co_owns_property_with", reporter=me,
               data="land_registry", count=n, evidence=f"{n} title(s) registered to both")
    return me


def build_graph(folder: Path) -> Graph:
    """The graph of every saved profile in `folder` (read through its index.json)."""
    b = _Builder()
    saved = 0
    analytics: dict[str, list[str]] = {}
    for entry in load_index(folder)["companies"]:
        if not entry.get("file"):
            continue
        profile = load_profile_file(folder / entry["file"])
        if profile is None:
            continue
        saved += 1
        me = add_profile(b, profile, entry["file"])
        for aid in profile.website.analytics_ids:
            analytics.setdefault(aid, []).append(me)
    for aid, ids in analytics.items():
        for i, x in enumerate(ids):
            for y in ids[i + 1:]:
                b.link(x, y, "shares_analytics_with", reporter=x, data="website", evidence=aid)
    b.resolve()
    nodes = sorted(b.nodes.values(), key=lambda n: (not n.saved, n.kind, n.name.lower()))
    edges = sorted(b.edges.values(), key=lambda e: (e.type, e.source, e.target))
    return Graph(companies_saved=saved, nodes=nodes, edges=edges)


def save_graph(folder: Path, graph: Graph | None = None) -> tuple[Graph, Path]:
    graph = graph or build_graph(folder)
    path = folder / GRAPH_NAME
    write_atomic(path, graph.model_dump_json(indent=2))
    return graph, path


# --- questions about the graph -----------------------------------------------------------


def find_node(graph: Graph, query: str) -> Node | None:
    """A node by company number, id, or exact (legal) name."""
    num = _number(query)
    if num and graph.node(f"ch:{num}"):
        return graph.node(f"ch:{num}")
    if graph.node(query):
        return graph.node(query)
    key = legal_key(query)
    return next((n for n in graph.nodes if legal_key(n.name) == key), None)


def _adjacency(graph: Graph) -> dict[str, list[tuple[str, Edge]]]:
    adj: dict[str, list[tuple[str, Edge]]] = {}
    for e in graph.edges:
        adj.setdefault(e.source, []).append((e.target, e))
        adj.setdefault(e.target, []).append((e.source, e))
    return adj


def neighbours(graph: Graph, node_id: str) -> list[dict[str, Any]]:
    """Direct links of one node: [{"edge": Edge, "other": Node, "outgoing": bool}]."""
    out = []
    for other, e in _adjacency(graph).get(node_id, []):
        out.append({"edge": e, "other": graph.node(other), "outgoing": e.source == node_id})
    return sorted(out, key=lambda x: (x["edge"].type, x["other"].name.lower()))


def connections(graph: Graph, node_id: str, max_hops: int = 2) -> list[dict[str, Any]]:
    """Other saved companies reachable within `max_hops`, with the shortest path to each:
    [{"company": Node, "path": [(Edge, Node), ...]}], nearest first."""
    adj = _adjacency(graph)
    seen = {node_id: None}
    queue = deque([(node_id, 0)])
    found = []
    while queue:
        current, depth = queue.popleft()
        if depth >= max_hops:
            continue
        for other, e in adj.get(current, []):
            if other in seen:
                continue
            seen[other] = (current, e)
            node = graph.node(other)
            if node is not None and node.saved:
                found.append(other)
            queue.append((other, depth + 1))
    results = []
    for target in found:
        path, step = [], target
        while seen[step] is not None:
            prev, e = seen[step]
            path.append((e, graph.node(step)))
            step = prev
        results.append({"company": graph.node(target), "path": list(reversed(path))})
    return sorted(results, key=lambda r: (len(r["path"]), r["company"].name.lower()))


def describe_edge(e: Edge, graph: Graph) -> str:
    """'MONZO BANK owns X' style text."""
    s, t = graph.node(e.source), graph.node(e.target)
    verb = e.type.replace("_", " ")
    extra = []
    if e.count:
        extra.append(f"x{e.count}")
    if e.value:
        extra.append(f"£{e.value:,.0f}")
    return f"{s.name if s else e.source} {verb} {t.name if t else e.target}" + (
        f" ({', '.join(extra)})" if extra else "")


def stats(graph: Graph) -> dict[str, Any]:
    kinds: dict[str, int] = {}
    for n in graph.nodes:
        kinds[n.kind] = kinds.get(n.kind, 0) + 1
    types: dict[str, int] = {}
    for e in graph.edges:
        types[e.type] = types.get(e.type, 0) + 1
    return {"companies_saved": graph.companies_saved, "nodes": len(graph.nodes),
            "edges": len(graph.edges), "node_kinds": kinds, "edge_types": types}
