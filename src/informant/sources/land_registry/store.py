"""The local SQLite copy of a dataset, and keeping it current with monthly files."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime
from itertools import pairwise
from pathlib import Path

import httpx

from ...errors import ConfigError, UpstreamError
from ...schema import utcnow
from .api import LandRegistryApi
from .common import (
    BATCH,
    CHECK_EVERY,
    UK,
    Notify,
    api_key,
    data_dir,
    db_path,
)
from .records import Row, read_rows

# --- the local database ---------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS titles (
    title_number TEXT PRIMARY KEY, tenure TEXT, address TEXT, district TEXT, county TEXT,
    region TEXT, postcode TEXT, multiple INTEGER, price REAL, added TEXT, owners TEXT);
CREATE TABLE IF NOT EXISTS owners (key TEXT, title_number TEXT);
"""
_INDEXES = """
CREATE INDEX IF NOT EXISTS owners_key ON owners (key);
CREATE INDEX IF NOT EXISTS owners_title ON owners (title_number);
"""


class PropertyStore:
    """The local copy of the dataset."""

    def __init__(self, path: Path | None = None, dataset: str = UK):
        self.dataset = dataset
        self.path = path or db_path(dataset)

    def exists(self) -> bool:
        return self.path.is_file()

    def connect(self, path: Path | None = None) -> sqlite3.Connection:
        conn = sqlite3.connect(path or self.path)
        conn.executescript(_SCHEMA)
        return conn

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        """A connection that is committed and, unlike `with sqlite3.connect()`, closed:
        Windows can't replace a database file that still has a connection open."""
        conn = self.connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def meta(self) -> dict[str, str]:
        if not self.exists():
            return {}
        with self.session() as conn:
            return dict(conn.execute("SELECT key, value FROM meta").fetchall())

    def set_meta(self, conn: sqlite3.Connection, **values) -> None:
        conn.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                         [(k, "" if v is None else str(v)) for k, v in values.items()])

    @staticmethod
    def _insert(conn: sqlite3.Connection, rows: list[Row]) -> None:
        conn.executemany("INSERT OR REPLACE INTO titles VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         [r.values for r in rows])
        conn.executemany("INSERT INTO owners VALUES (?, ?)",
                         [(k, r.title_number) for r in rows for k in r.owner_keys])

    def build(self, rows: Iterable[Row], *, dataset: str, month: str, published: date | None,
              notify: Notify | None = None) -> int:
        """A fresh database from a full file, swapped in only when complete."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.dataset}.", suffix=".sqlite", dir=self.path.parent)
        os.close(fd)
        tmp = Path(tmp_name)
        count = 0
        try:
            conn = self.connect(tmp)
            conn.execute("PRAGMA journal_mode=OFF")
            conn.execute("PRAGMA synchronous=OFF")
            batch: list[Row] = []
            for row in rows:
                batch.append(row)
                if len(batch) >= BATCH:
                    self._insert(conn, batch)
                    count += len(batch)
                    batch = []
                    if notify and count % 500_000 == 0:
                        notify(f"loaded {count:,} titles")
            self._insert(conn, batch)
            count += len(batch)
            if notify:
                notify(f"loaded {count:,} titles; indexing")
            conn.executescript(_INDEXES)
            self.set_meta(conn, dataset=dataset, month=month, base=dataset, changes="",
                          published=published.isoformat() if published else None,
                          titles=count, updated_at=utcnow().isoformat(),
                          checked_at=utcnow().isoformat())
            conn.commit()
            conn.close()
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)
        return count

    def apply_changes(self, rows: Iterable[Row], *, dataset: str, month: str,
                      published: date | None) -> tuple[int, int]:
        """Apply a change-only file: deletions first, then additions (a changed title
        appears as a deletion of the old record and an addition of the new one)."""
        rows = list(rows)
        deleted = [r.title_number for r in rows if r.change == "D"]
        added = [r for r in rows if r.change != "D"]
        with self.session() as conn:
            conn.executescript(_INDEXES)
            for i in range(0, len(deleted), 900):
                chunk = deleted[i:i + 900]
                marks = ",".join("?" * len(chunk))
                conn.execute(f"DELETE FROM titles WHERE title_number IN ({marks})", chunk)
                conn.execute(f"DELETE FROM owners WHERE title_number IN ({marks})", chunk)
            # an addition replaces any record of the same title still present
            for i in range(0, len(added), 900):
                chunk = [r.title_number for r in added[i:i + 900]]
                conn.execute(f"DELETE FROM owners WHERE title_number IN ({','.join('?' * len(chunk))})",
                             chunk)
            self._insert(conn, added)
            old = dict(conn.execute("SELECT key, value FROM meta").fetchall())
            titles = conn.execute("SELECT COUNT(*) FROM titles").fetchone()[0]
            applied = ",".join(x for x in (old.get("changes"), dataset) if x)
            self.set_meta(conn, dataset=dataset, month=month, changes=applied, titles=titles,
                          published=published.isoformat() if published else None,
                          updated_at=utcnow().isoformat(), checked_at=utcnow().isoformat())
        return len(added), len(deleted)

    def mark_checked(self) -> None:
        with self.session() as conn:
            self.set_meta(conn, checked_at=utcnow().isoformat())

    def titles_for(self, keys: list[str]) -> list[tuple[str, sqlite3.Row]]:
        """(matched key, title row) for each title owned under any of the keys."""
        if not keys or not self.exists():
            return []
        conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            out, seen = [], set()
            for key in keys:
                for row in conn.execute(
                        "SELECT t.* FROM owners o JOIN titles t ON t.title_number = o.title_number "
                        "WHERE o.key = ?", (key,)):
                    if row["title_number"] not in seen:
                        seen.add(row["title_number"])
                        out.append((key, row))
            return out
        finally:
            conn.close()


# --- keeping it current ---------------------------------------------------------------------


def update(store: PropertyStore | None = None, api: LandRegistryApi | None = None, *,
           full: bool = False, allow_full: bool = True, notify: Notify | None = None) -> dict:
    """Bring the local copy up to the latest month. Returns what was done:
    {"action": "none" | "changes" | "full" | "needs_full", "month", "files", ...}."""
    store = store or PropertyStore()
    ds = store.dataset
    own_api = api is None
    api = api or LandRegistryApi()
    try:
        latest = api.latest(ds)
        current = store.meta().get("month") if store.exists() else None
        if not full and current and current >= latest["month"]:
            store.mark_checked()
            return {"action": "none", "month": current, "files": []}
        plan: list[dict] = []
        if not full and current:
            history = api.history(ds)
            months = sorted({f["month"] for f in history if f["month"] > current})
            changes = {f["month"]: f for f in history if f["kind"] == "COU"}
            if latest.get("change") and latest["month"] not in changes:
                changes[latest["month"]] = {"file": latest["change"], "month": latest["month"],
                                            "published": latest.get("published")}
            months = sorted(set(months) | {latest["month"]})
            # change files only work as an unbroken monthly chain from our month
            if all(m in changes for m in months) and _consecutive([current] + months):
                plan = [changes[m] for m in months]
        if plan:
            work = data_dir() / "downloads"
            for f in plan:
                path = api.download(f["file"], work / f["file"], notify)
                try:
                    added, deleted = store.apply_changes(read_rows(path), dataset=f["file"],
                                                         month=f["month"], published=f["published"])
                finally:
                    path.unlink(missing_ok=True)
                if notify:
                    notify(f"{f['file']}: {added:,} added, {deleted:,} deleted")
            return {"action": "changes", "month": plan[-1]["month"], "files": [f["file"] for f in plan]}
        if not allow_full:
            return {"action": "needs_full", "month": current, "files": [latest["full"]]}
        path = api.download(latest["full"], data_dir() / "downloads" / latest["full"], notify)
        try:
            count = store.build(read_rows(path), dataset=latest["full"], month=latest["month"],
                                published=latest.get("published"), notify=notify)
        finally:
            path.unlink(missing_ok=True)
        return {"action": "full", "month": latest["month"], "files": [latest["full"]], "titles": count}
    finally:
        if own_api:
            api.close()


def _consecutive(months: list[str]) -> bool:
    def index(m: str) -> int:
        y, mo = m.split("-")
        return int(y) * 12 + int(mo)
    return all(index(b) - index(a) == 1 for a, b in pairwise(months))


def keep_current(store: PropertyStore, notify: Notify | None = None) -> str | None:
    """For lookups: at most once a day, apply any new change files (never the full file).
    Returns a note when the copy is out of date or the check failed."""
    meta = store.meta()
    try:
        checked = datetime.fromisoformat(meta.get("checked_at") or "")
    except ValueError:
        checked = None
    if checked is not None and utcnow() - checked < CHECK_EVERY:
        return None
    if not api_key():
        return None
    try:
        result = update(store, full=False, allow_full=False, notify=notify)
    except (ConfigError, UpstreamError, httpx.HTTPError, OSError, sqlite3.Error) as e:
        return f"could not check for newer Land Registry data: {e}"
    if result["action"] == "needs_full":
        return ("the local Land Registry copy is too old to update with change files: run "
                "`informant property --update --full`")
    return None


