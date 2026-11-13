"""Local cache of crawled pages and LLM answers.

Why: local models take minutes per call, and websites shouldn't be re-fetched
needlessly. With the cache, a re-run is near-instant, a run that failed half
way resumes where it stopped, and old companies can be re-extracted with a
better model or prompt without crawling again.

Layout (INFORMANT_CACHE_DIR, default ./.informant_cache):
    pages/<hash>.json.gz   one crawled page: url, final_url, html, headers, fetched_at
    llm/<hash>.json        one model answer, keyed by model + prompts + schema
    accounts/<hash>.json   figures parsed from one filed accounts document

Pages expire after INFORMANT_CACHE_DAYS (default 7). LLM answers never expire:
their key includes the model, the prompts and the exact text sent, so any change
to those makes a new entry. INFORMANT_CACHE=off disables everything.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .schema import utcnow
from .storage import write_atomic

DEFAULT_DIR = ".informant_cache"
DEFAULT_MAX_AGE_DAYS = 7.0
_OFF = {"0", "off", "false", "no"}
CACHE_FOLDERS = ("pages", "llm", "accounts")


def cache_enabled() -> bool:
    return os.environ.get("INFORMANT_CACHE", "on").strip().lower() not in _OFF


def cache_root() -> Path:
    return Path(os.environ.get("INFORMANT_CACHE_DIR") or DEFAULT_DIR)


def _hash(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:40]


# --- pages --------------------------------------------------------------------


@dataclass
class CachedPage:
    url: str
    final_url: str
    status_code: int
    html: str
    headers: dict[str, str]
    rendered: bool
    fetched_at: datetime


class PageCache:
    def __init__(self, root: Path | None = None, *, max_age_days: float | None = None,
                 refresh: bool = False) -> None:
        self.dir = (root or cache_root()) / "pages"
        if max_age_days is None:
            max_age_days = float(os.environ.get("INFORMANT_CACHE_DAYS", DEFAULT_MAX_AGE_DAYS))
        self.max_age = timedelta(days=max_age_days)
        self.refresh = refresh  # don't read (re-fetch everything), but still write

    def _path(self, url: str) -> Path:
        return self.dir / f"{_hash(url.strip().rstrip('/').lower())}.json.gz"

    def get(self, url: str, max_age: timedelta | None = None) -> CachedPage | None:
        """`max_age` overrides the default lifetime (robots.txt is kept for less)."""
        if self.refresh:
            return None
        try:
            with gzip.open(self._path(url), "rt", encoding="utf-8") as f:
                d = json.load(f)
            page = CachedPage(
                url=d["url"], final_url=d["final_url"], status_code=d["status_code"],
                html=d["html"], headers=d.get("headers") or {}, rendered=bool(d.get("rendered")),
                fetched_at=datetime.fromisoformat(d["fetched_at"]),
            )
        except (OSError, ValueError, KeyError, EOFError):
            return None  # missing or damaged entry: treat as a miss
        if utcnow() - page.fetched_at > (max_age if max_age is not None else self.max_age):
            return None
        return page

    def put(self, url: str, *, final_url: str, status_code: int, html: str,
            headers: dict[str, str], rendered: bool, fetched_at: datetime) -> None:
        payload = json.dumps({
            "url": url, "final_url": final_url, "status_code": status_code, "html": html,
            "headers": headers, "rendered": rendered, "fetched_at": fetched_at.isoformat(),
        })
        path = self._path(url)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with gzip.open(tmp, "wt", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, path)
        except OSError:
            pass  # a cache that can't be written must never break a lookup


# --- LLM answers --------------------------------------------------------------


def llm_key(model_id: str, system: str, user: str, schema: type[BaseModel]) -> str:
    return _hash(model_id, system, user, json.dumps(schema.model_json_schema(), sort_keys=True))


class JsonCache:
    """A folder of small JSON files that never expire: for results that are a pure
    function of their key (an LLM answer for an exact request; figures parsed
    from a filed document, which never changes)."""

    name = "json"

    def __init__(self, root: Path | None = None) -> None:
        self.dir = (root or cache_root()) / self.name

    def _file(self, key: str) -> Path:
        # Keys may be URLs or hashes; hash anything that isn't a safe file name.
        safe = key if key.isalnum() and len(key) <= 64 else _hash(key)
        return self.dir / f"{safe}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        try:
            return json.loads(self._file(key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def put(self, key: str, payload: dict[str, Any]) -> None:
        try:
            write_atomic(self._file(key), json.dumps(payload, ensure_ascii=False))
        except OSError:
            pass


class LlmCache(JsonCache):
    name = "llm"


class AccountsCache(JsonCache):
    """Figures parsed from a filed accounts document, keyed by the document's URL."""

    name = "accounts"


# --- housekeeping ---------------------------------------------------------------


def cache_stats(root: Path | None = None) -> dict[str, Any]:
    root = root or cache_root()
    stats: dict[str, Any] = {"path": str(root.resolve()), "enabled": cache_enabled()}
    for name in CACHE_FOLDERS:
        files = [f for f in (root / name).glob("*") if f.is_file()] if (root / name).is_dir() else []
        stats[name] = {"entries": len(files), "bytes": sum(f.stat().st_size for f in files)}
    return stats


def clear_cache(root: Path | None = None, *, pages: bool = True, llm: bool = True) -> int:
    """Delete cached entries; returns how many files were removed."""
    root = root or cache_root()
    removed = 0
    for name, wanted in (("pages", pages), ("llm", llm), ("accounts", pages)):
        folder = root / name
        if not wanted or not folder.is_dir():
            continue
        for f in folder.glob("*"):
            if f.is_file():
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
    return removed
