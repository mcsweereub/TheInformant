"""Saving profiles to disk, and the index.json that lists them.

Files are written atomically (to a temporary file, then renamed), so a reader
such as a GUI never sees a half-written profile, even if a lookup is cancelled.

Each output folder has an index.json summarising every profile in it, so a
program can list companies without opening every file.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .schema import SCHEMA_VERSION, CompanyProfile, utcnow

INDEX_NAME = "index.json"
INDEX_VERSION = 1


def default_output_dir() -> Path:
    return Path(os.environ.get("INFORMANT_OUTPUT_DIR") or "output")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def profile_filename(profile: CompanyProfile) -> str:
    number = profile.identity.company_number or "unknown"
    return f"{number}-{slug(profile.identity.name)}.json"


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def index_entry(profile: CompanyProfile, file_name: str,
                history: dict[str, Any] | None = None) -> dict[str, Any]:
    ident = profile.identity
    latest = profile.financials.periods[0] if profile.financials.periods else None
    return {
        "company_number": ident.company_number,
        "name": ident.name,
        "status": ident.status,
        "incorporated_on": ident.incorporated_on.isoformat() if ident.incorporated_on else None,
        "website": ident.website,
        "description": ident.description,
        "sectors": profile.classification.sectors,
        "sic_codes": [s.code for s in profile.classification.sic_codes],
        "llm_model": profile.website.llm_model,
        "wikipedia": profile.wikipedia.url,
        "first_year": profile.history[0].year if profile.history else None,
        "milestones": len(profile.history),
        "funding_rounds": len(profile.funding.rounds),
        # who this company sells to (matched UK customers have a number); "customer_of"
        # is worked out across the whole folder when the index is written
        "customers": [{"name": c.name, "company_number": c.company_number}
                      for c in profile.customers],
        "lei": profile.lei.lei,
        "public_contracts": profile.public_contracts.found,
        "political_donations": profile.political_donations.found,
        "political_donations_value": profile.political_donations.total_value,
        "property_titles": profile.property.found if profile.property.dataset else None,
        "property_price_paid": profile.property.total_price_paid,
        "open_roles": profile.hiring.total,
        "email_provider": profile.domain.email_provider,
        # sites sharing one of these IDs are usually run by the same organisation;
        # "shares_analytics_with" is worked out across the folder when the index is written
        "analytics_ids": profile.website.analytics_ids,
        "news_count": sum(1 for n in profile.news.items if n.relevant is not False),
        "latest_news_at": max((n.published_at.isoformat() for n in profile.news.items
                               if n.published_at), default=None),
        # headline registry figures, so a list view needn't open every profile
        "accounts_period_end": latest.period_end.isoformat() if latest else None,
        "turnover": latest.turnover if latest else None,
        "net_assets": latest.net_assets if latest else None,
        "employees": latest.employees if latest else None,
        "ultimate_parent": next((p.name for p in reversed(profile.ownership.parents)
                                 if p.is_ultimate), None),
        "flags": {sev: sum(1 for f in profile.risk.flags if f.severity == sev)
                  for sev in ("high", "warning", "info")},
        "schema_version": profile.schema_version,
        "generated_at": profile.generated_at.isoformat(),
        "file": file_name,
        # change tracking (see history/): lookups kept, and what the latest one changed
        **(history or {"lookups": 0, "previous_lookup_at": None, "changes_since_previous": None}),
    }


def _empty_index() -> dict[str, Any]:
    return {"index_version": INDEX_VERSION, "schema_version": SCHEMA_VERSION,
            "updated_at": None, "companies": []}


def link_customers(companies: list[dict[str, Any]]) -> None:
    """Fill each entry's "customer_of" from the other entries' customer lists, so the
    links are right whatever order the companies were researched in."""
    suppliers: dict[str, list[dict[str, Any]]] = {}
    for entry in companies:
        for customer in entry.get("customers") or []:
            number = customer.get("company_number")
            if number and entry.get("company_number"):
                suppliers.setdefault(number, []).append(
                    {"name": entry.get("name"), "company_number": entry.get("company_number")})
    for entry in companies:
        entry["customer_of"] = suppliers.get(entry.get("company_number") or "", [])


def link_shared_analytics(companies: list[dict[str, Any]]) -> None:
    """Fill each entry's "shares_analytics_with": other companies in the folder whose
    sites carry the same analytics or tag ID (a strong sign of common ownership)."""
    by_id: dict[str, list[dict[str, Any]]] = {}
    for entry in companies:
        for aid in entry.get("analytics_ids") or []:
            by_id.setdefault(aid, []).append(entry)
    for entry in companies:
        shared: dict[str, dict[str, Any]] = {}
        for aid in entry.get("analytics_ids") or []:
            for other in by_id.get(aid, []):
                if other is entry or other.get("company_number") == entry.get("company_number"):
                    continue
                key = other.get("company_number") or other.get("name") or ""
                link = shared.setdefault(key, {"name": other.get("name"),
                                               "company_number": other.get("company_number"),
                                               "ids": []})
                link["ids"].append(aid)
        entry["shares_analytics_with"] = list(shared.values())


def _write_index(folder: Path, index: dict[str, Any]) -> Path:
    index["updated_at"] = utcnow().isoformat()
    index["companies"].sort(key=lambda c: (c.get("name") or "").lower())
    link_customers(index["companies"])
    link_shared_analytics(index["companies"])
    path = folder / INDEX_NAME
    write_atomic(path, json.dumps(index, indent=2))
    return path


def load_index(folder: Path) -> dict[str, Any]:
    """Read index.json, rebuilding it from the profile files if missing or broken."""
    path = folder / INDEX_NAME
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(index, dict) and isinstance(index.get("companies"), list):
            return index
    except (OSError, ValueError):
        pass
    return rebuild_index(folder)


def load_saved_profile(folder: Path, company_number: str) -> tuple[CompanyProfile, Path] | None:
    """A previously saved profile from `folder`, found through its index."""
    for entry in load_index(folder)["companies"]:
        if entry.get("company_number") == company_number and entry.get("file"):
            path = folder / entry["file"]
            try:
                return CompanyProfile.model_validate_json(path.read_text(encoding="utf-8")), path
            except (OSError, ValidationError, ValueError):
                return None
    return None


def rebuild_index(folder: Path) -> dict[str, Any]:
    index = _empty_index()
    if folder.is_dir():
        for file in sorted(folder.glob("*.json")):
            if file.name == INDEX_NAME:
                continue
            try:
                profile = CompanyProfile.model_validate_json(file.read_text(encoding="utf-8"))
            except (OSError, ValidationError, ValueError):
                continue  # not a profile (or from an incompatible schema)
            index["companies"].append(index_entry(profile, file.name,
                                                  history_summary(folder, profile.identity.company_number)))
        _write_index(folder, index)
    return index


def save_profile(profile: CompanyProfile, *, out_dir: Path | None = None,
                 out_file: Path | None = None) -> tuple[Path, Path]:
    """Write the profile, keep a dated copy in history/ and update the folder's index.
    Returns (profile path, index path)."""
    path = out_file if out_file is not None else (out_dir or default_output_dir()) / profile_filename(profile)
    path = path.resolve()
    folder = path.parent
    number = profile.identity.company_number
    if number and history_enabled():
        if not list_snapshots(folder, number):
            # First save with history on: the profile already saved (if any) is the
            # starting point, so changes show from the very next lookup.
            saved = load_saved_profile(folder, number) if (folder / INDEX_NAME).exists() else None
            earlier = saved[0] if saved else load_profile_file(path)
            if (earlier is not None and earlier.identity.company_number == number
                    and earlier.generated_at != profile.generated_at):
                _keep_snapshot(folder, earlier)
        _keep_snapshot(folder, profile)
    write_atomic(path, profile.to_json())

    index = load_index(folder)
    index["companies"] = [
        c for c in index["companies"]
        if c.get("file") != path.name and (number is None or c.get("company_number") != number)
    ]
    index["companies"].append(index_entry(profile, path.name, history_summary(folder, number)))
    return path, _write_index(folder, index)


# --- history: a dated copy of every lookup, for change tracking ----------------------------

HISTORY_DIR = "history"
MAX_SNAPSHOTS = 50  # per company; the oldest go first


def history_enabled() -> bool:
    return os.environ.get("INFORMANT_HISTORY", "on").strip().lower() not in {"0", "off", "false", "no"}


def history_dir(folder: Path, company_number: str) -> Path:
    return folder / HISTORY_DIR / slug(company_number)


def list_snapshots(folder: Path, company_number: str | None) -> list[Path]:
    """The company's saved lookups, oldest first."""
    if not company_number:
        return []
    d = history_dir(folder, company_number)
    return sorted(d.glob("*.json")) if d.is_dir() else []


def load_profile_file(path: Path) -> CompanyProfile | None:
    try:
        return CompanyProfile.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError):
        return None


def _keep_snapshot(folder: Path, profile: CompanyProfile) -> None:
    d = history_dir(folder, profile.identity.company_number or "unknown")
    write_atomic(d / f"{profile.generated_at:%Y%m%dT%H%M%SZ}.json", profile.to_json())
    for old in sorted(d.glob("*.json"))[:-MAX_SNAPSHOTS]:
        old.unlink(missing_ok=True)


def compare_snapshots(folder: Path, company_number: str, since: datetime | None = None):
    """Changes between the latest saved lookup and the one before it (or the last one at
    or before `since`). None if there aren't two lookups to compare."""
    from .changes import compare

    snaps = list_snapshots(folder, company_number)
    if len(snaps) < 2:
        return None
    current = load_profile_file(snaps[-1])
    previous_path = snaps[-2]
    if since is not None:
        stamp = f"{since:%Y%m%dT%H%M%SZ}"
        earlier = [s for s in snaps[:-1] if s.stem <= stamp]
        previous_path = earlier[-1] if earlier else snaps[0]
    previous = load_profile_file(previous_path)
    if current is None or previous is None:
        return None
    return compare(previous, current)


def history_summary(folder: Path, company_number: str | None) -> dict[str, Any]:
    """For the index: how many lookups are kept and what changed in the last one."""
    snaps = list_snapshots(folder, company_number)
    comparison = compare_snapshots(folder, company_number) if len(snaps) >= 2 else None
    return {
        "lookups": len(snaps),
        "previous_lookup_at": comparison.previous_at.isoformat() if comparison else None,
        "changes_since_previous": len(comparison.changes) if comparison else None,
    }
