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


def index_entry(profile: CompanyProfile, file_name: str) -> dict[str, Any]:
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
    }


def _empty_index() -> dict[str, Any]:
    return {"index_version": INDEX_VERSION, "schema_version": SCHEMA_VERSION,
            "updated_at": None, "companies": []}


def _write_index(folder: Path, index: dict[str, Any]) -> Path:
    index["updated_at"] = utcnow().isoformat()
    index["companies"].sort(key=lambda c: (c.get("name") or "").lower())
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
            index["companies"].append(index_entry(profile, file.name))
        _write_index(folder, index)
    return index


def save_profile(profile: CompanyProfile, *, out_dir: Path | None = None,
                 out_file: Path | None = None) -> tuple[Path, Path]:
    """Write the profile and update the folder's index. Returns (profile path, index path)."""
    path = out_file if out_file is not None else (out_dir or default_output_dir()) / profile_filename(profile)
    path = path.resolve()
    write_atomic(path, profile.to_json())

    folder = path.parent
    index = load_index(folder)
    number = profile.identity.company_number
    index["companies"] = [
        c for c in index["companies"]
        if c.get("file") != path.name and (number is None or c.get("company_number") != number)
    ]
    index["companies"].append(index_entry(profile, path.name))
    return path, _write_index(folder, index)
