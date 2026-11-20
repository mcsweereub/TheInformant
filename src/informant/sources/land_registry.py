"""Property owned by the company: HM Land Registry's "UK companies that own property in
England and Wales" dataset (formerly CCOD), through its official API.

The dataset is published monthly as one full file (about 4.5 million titles, ~1.6 GB of
CSV) plus a change-only file (titles added and deleted that month). A lookup can't read a
1.6 GB CSV each time, so the full file is downloaded once into a local SQLite database
keyed by company number, then kept current with the monthly change files:

    informant property --update          download (first time) or apply new change files
    informant property --update --full   rebuild from the latest full file

Lookups read the local copy only; they apply a pending change file themselves (seconds)
but never download the full file. Needs a free account and API key
(LAND_REGISTRY_API_KEY) from use-land-property-data.service.gov.uk, with the dataset's
licence accepted. The dataset covers titles owned by companies and other organisations
only, never private individuals.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
import tempfile
import zipfile
from contextlib import contextmanager
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

from ..errors import ConfigError, UpstreamError
from ..schema import (
    CoOwner,
    GroupProperty,
    PropertyHoldings,
    PropertyTitle,
    Source,
    utcnow,
)
from .fetchutil import api_client
from .website.extract import legal_key

API = "https://use-land-property-data.service.gov.uk/api/v1"
DATASET = "ccod"
DATASET_PAGE = "https://use-land-property-data.service.gov.uk/datasets/ccod"
DB_NAME = "land_registry_ccod.sqlite"
MAX_ITEMS = 250  # titles listed per company (totals always cover all of them)
CHECK_EVERY = timedelta(days=1)  # how often a lookup asks the API for a newer month
BATCH = 50_000
OWNERS = 4  # the CSV has up to four proprietors per title
_FILE_RE = re.compile(r"^CCOD_(FULL|COU)_(\d{4})_(\d{2})\.zip$", re.I)

Notify = Callable[[str], None]


def data_dir() -> Path:
    """Downloaded datasets (not a cache: `informant cache --clear` leaves them alone)."""
    return Path(os.environ.get("INFORMANT_DATA_DIR") or ".informant_data")


def db_path() -> Path:
    return data_dir() / DB_NAME


def api_key() -> str | None:
    return (os.environ.get("LAND_REGISTRY_API_KEY") or "").strip() or None


def file_month(file_name: str) -> str | None:
    """'CCOD_COU_2026_11.zip' -> '2026-11'."""
    m = _FILE_RE.match(file_name or "")
    return f"{m.group(2)}-{m.group(3)}" if m else None


def number_key(number: str | None) -> str:
    """Company numbers in the dataset lose their leading zeros ('9446231' for 09446231)."""
    return (number or "").strip().upper().replace(" ", "").lstrip("0")


def _date(text: str | None) -> date | None:
    for fmt in ("%d-%m-%Y", "%d %B %Y"):
        try:
            return datetime.strptime((text or "").strip(), fmt).date()
        except ValueError:
            continue
    return None


def _price(text: str | None) -> float | None:
    try:
        value = float((text or "").replace(",", "").replace("£", "").strip())
    except ValueError:
        return None
    return value if value > 0 else None


# --- the API --------------------------------------------------------------------------------


class LandRegistryApi:
    def __init__(self, key: str | None = None, http: httpx.Client | None = None):
        key = key or api_key()
        if not key:
            raise ConfigError("LAND_REGISTRY_API_KEY is not set: create a free account at "
                              "use-land-property-data.service.gov.uk, accept the licence for "
                              "the UK companies dataset and add the key to .env")
        self.http = http or api_client(timeout=60.0, headers={"Authorization": key,
                                                              "Accept": "application/json"})

    def close(self) -> None:
        self.http.close()

    def _get(self, path: str) -> dict:
        resp = self.http.get(API + path)
        if resp.status_code in (401, 403):
            raise ConfigError("HM Land Registry refused the API key (check LAND_REGISTRY_API_KEY "
                              "and that the dataset's licence is accepted on your account)")
        if resp.status_code != 200:
            raise UpstreamError(f"HM Land Registry API: HTTP {resp.status_code} for {path}")
        return resp.json()

    def latest(self) -> dict:
        """{"full": file name, "change": file name, "month": "YYYY-MM", "published": date}."""
        result = self._get(f"/datasets/{DATASET}").get("result") or {}
        out: dict = {"published": _date(result.get("last_updated"))}
        for res in result.get("resources") or []:
            name = res.get("file_name") or ""
            m = _FILE_RE.match(name)
            if m:
                out["full" if m.group(1).upper() == "FULL" else "change"] = name
                out["month"] = file_month(name)
        if "full" not in out:
            raise UpstreamError("HM Land Registry API: no full file listed for the dataset")
        return out

    def history(self) -> list[dict]:
        """Every published file: [{"file", "kind" (FULL/COU), "month", "published"}], oldest first."""
        data = self._get(f"/datasets/history/{DATASET}")
        files = []
        for item in data.get("dataset_history") or []:
            name = item.get("filename") or ""
            m = _FILE_RE.match(name)
            if m:
                files.append({"file": name, "kind": m.group(1).upper(), "month": file_month(name),
                              "published": _date(item.get("unsorted_date"))})
        return sorted(files, key=lambda f: (f["month"], f["kind"]))

    def download(self, file_name: str, dest: Path, notify: Notify | None = None) -> Path:
        """Fetch a file: the API gives a signed link valid for ~10 seconds, so the download
        starts straight away."""
        link = (self._get(f"/datasets/{DATASET}/{file_name}").get("result") or {}).get("download_url")
        if not link:
            raise UpstreamError(f"HM Land Registry API: no download link for {file_name}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        # the signed link carries its own credentials: never send the API key to storage
        with httpx.stream("GET", link, timeout=httpx.Timeout(60.0, read=300.0),
                          follow_redirects=True) as resp:
            if resp.status_code != 200:
                raise UpstreamError(f"downloading {file_name}: HTTP {resp.status_code}")
            total = int(resp.headers.get("content-length") or 0)
            done, shown = 0, -1
            with open(tmp, "wb") as f:
                for chunk in resp.iter_bytes(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    step = done * 10 // total if total else -1
                    if notify and total and step != shown:
                        shown = step
                        notify(f"downloading {file_name}: {done / 1_048_576:.0f} of "
                               f"{total / 1_048_576:.0f} MB")
        os.replace(tmp, dest)
        return dest


# --- reading the CSV ------------------------------------------------------------------------


@dataclass
class Row:
    title_number: str
    values: tuple  # for the titles table
    owner_keys: list[str]  # lookup keys: "n:<number>", or "k:<legal name>" when no number
    change: str | None  # A / D in change files


def read_rows(zip_path: Path) -> Iterator[Row]:
    with zipfile.ZipFile(zip_path) as z:
        name = next((n for n in z.namelist() if n.lower().endswith(".csv")), None)
        if name is None:
            raise UpstreamError(f"{zip_path.name} holds no CSV file")
        with z.open(name) as raw:
            reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
            for rec in reader:
                row = parse_record(rec)
                if row is not None:
                    yield row


def parse_record(rec: dict) -> Row | None:
    title = (rec.get("Title Number") or "").strip()
    if not title or rec.get("Tenure") is None:  # footer lines ("Row Count: ...")
        return None
    owners, keys = [], []
    for i in range(1, OWNERS + 1):
        name = (rec.get(f"Proprietor Name ({i})") or "").strip()
        if not name:
            continue
        number = (rec.get(f"Company Registration No. ({i})") or "").strip()
        address = ", ".join(a.strip() for a in (rec.get(f"Proprietor ({i}) Address ({j})")
                                                for j in (1, 2, 3)) if a and a.strip())
        owners.append([name, number, (rec.get(f"Proprietorship Category ({i})") or "").strip(), address])
        key = f"n:{number_key(number)}" if number_key(number) else f"k:{legal_key(name)}"
        if key not in keys and len(key) > 2:
            keys.append(key)
    added = _date(rec.get("Date Proprietor Added"))
    values = (title, (rec.get("Tenure") or "").strip(), (rec.get("Property Address") or "").strip(),
              (rec.get("District") or "").strip(), (rec.get("County") or "").strip(),
              (rec.get("Region") or "").strip(), (rec.get("Postcode") or "").strip(),
              1 if (rec.get("Multiple Address Indicator") or "").strip().upper() == "Y" else 0,
              _price(rec.get("Price Paid")), added.isoformat() if added else None,
              json.dumps(owners, ensure_ascii=False))
    change = (rec.get("Change Indicator") or "").strip().upper() or None
    return Row(title, values, keys, change)


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

    def __init__(self, path: Path | None = None):
        self.path = path or db_path()

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
        fd, tmp_name = tempfile.mkstemp(prefix=".ccod.", suffix=".sqlite", dir=self.path.parent)
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
    own_api = api is None
    api = api or LandRegistryApi()
    try:
        latest = api.latest()
        current = store.meta().get("month") if store.exists() else None
        if not full and current and current >= latest["month"]:
            store.mark_checked()
            return {"action": "none", "month": current, "files": []}
        plan: list[dict] = []
        if not full and current:
            history = api.history()
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
    return all(index(b) - index(a) == 1 for a, b in zip(months, months[1:]))


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


# --- one company ----------------------------------------------------------------------------


def _title(row: sqlite3.Row, own_keys: set[str], matched_by: str) -> PropertyTitle:
    owners = json.loads(row["owners"] or "[]")
    mine, others = None, []
    for name, number, category, address in owners:
        key = f"n:{number_key(number)}" if number_key(number) else f"k:{legal_key(name)}"
        if mine is None and key in own_keys:
            mine = address
        else:
            others.append(CoOwner(name=name, company_number=number or None, category=category or None))
    return PropertyTitle(
        title_number=row["title_number"], tenure=row["tenure"] or None, address=row["address"] or None,
        district=row["district"] or None, county=row["county"] or None, region=row["region"] or None,
        postcode=row["postcode"] or None, multiple_addresses=bool(row["multiple"]),
        price_paid=row["price"], owner_since=date.fromisoformat(row["added"]) if row["added"] else None,
        owner_address=mine or None, co_owners=others, matched_by=matched_by)


def name_variants(name: str) -> list[str]:
    """Exact-name keys for one name, allowing only for how initials are written: the
    register has both "J C BAMFORD EXCAVATORS" and "J.C. BAMFORD EXCAVATORS"."""
    spaced = legal_key(name.replace(".", " "))
    tokens, joined, run = spaced.split(), [], ""
    for tok in tokens:
        if len(tok) == 1 and tok.isalpha():
            run += tok
            continue
        if run:
            joined.append(run)
            run = ""
        joined.append(tok)
    if run:
        joined.append(run)
    return [k for k in dict.fromkeys([legal_key(name), spaced, " ".join(joined)]) if k]


def _keys(company_number: str | None, names: Iterable[str]) -> tuple[list[str], list[str]]:
    by_number = [f"n:{number_key(company_number)}"] if number_key(company_number) else []
    by_name = list(dict.fromkeys(f"k:{key}" for n in names if n for key in name_variants(n)))
    return by_number, by_name


def fetch_property(company_number: str | None, names: Iterable[str] = (),
                   store: PropertyStore | None = None) -> PropertyHoldings:
    """Titles registered to the company: by company number, plus titles whose owner entry
    has no number but exactly the company's (current or previous) name."""
    store = store or PropertyStore()
    holdings = PropertyHoldings()
    if not store.exists():
        holdings.notes.append("no local copy of the Land Registry dataset: run "
                              "`informant property --update` (downloads ~1.6 GB of data once)")
        return holdings
    meta = store.meta()
    holdings.dataset = meta.get("dataset") or None
    holdings.data_as_of = _date_iso(meta.get("published"))
    holdings.sources = [Source(name="land_registry", url=DATASET_PAGE,
                               retrieved_at=_datetime_iso(meta.get("updated_at")) or utcnow())]
    by_number, by_name = _keys(company_number, names)
    own = set(by_number + by_name)
    rows = store.titles_for(by_number + by_name)
    titles = [_title(row, own, "company_number" if key.startswith("n:") else "name")
              for key, row in rows]
    _summarise(holdings, titles)
    holdings.items = sorted(titles, key=lambda t: (t.owner_since or date.min, t.title_number),
                            reverse=True)[:MAX_ITEMS]
    named = sum(1 for t in titles if t.matched_by == "name")
    if named:
        holdings.notes.append(f"{named} title(s) matched on the exact name (no company number "
                              "on the register entry)")
    if len(titles) > MAX_ITEMS:
        holdings.notes.append(f"{len(titles)} titles: the newest {MAX_ITEMS} are listed, "
                              "the totals cover all of them")
    return holdings


def _summarise(target, titles: list[PropertyTitle]) -> None:
    tenure = Counter((t.tenure or "").lower() for t in titles)
    prices = [t.price_paid for t in titles if t.price_paid]
    dates = [t.owner_since for t in titles if t.owner_since]
    if hasattr(target, "found"):
        target.found = len(titles)
        target.priced_titles = len(prices)
        target.first_acquired = min(dates, default=None)
        target.last_acquired = max(dates, default=None)
    else:
        target.titles = len(titles)
    target.freehold = tenure.get("freehold", 0)
    target.leasehold = tenure.get("leasehold", 0)
    target.total_price_paid = round(sum(prices), 2) if prices else None
    target.regions = dict(Counter(t.region for t in titles if t.region).most_common())


def fetch_group_property(companies: list[tuple[str, str, str]],
                         store: PropertyStore | None = None) -> list[GroupProperty]:
    """Totals for other group companies (number, name, relation), matched on number only."""
    store = store or PropertyStore()
    out = []
    for number, name, relation in companies:
        by_number, _ = _keys(number, ())
        own = set(by_number)
        titles = [_title(row, own, "company_number") for _, row in store.titles_for(by_number)]
        if titles:
            g = GroupProperty(name=name, company_number=number, relation=relation)
            _summarise(g, titles)
            out.append(g)
    return out


def _date_iso(text: str | None) -> date | None:
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


def _datetime_iso(text: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(text) if text else None
    except ValueError:
        return None


def property_material(holdings: PropertyHoldings, limit: int = 15):
    """LLM material: where the company owns land or buildings, from the register."""
    from .website.parse import Page

    if not holdings.found:
        return None
    lines = [f"The company owns {holdings.found} registered title(s) in England and Wales "
             f"({holdings.freehold} freehold, {holdings.leasehold} leasehold)."]
    for t in holdings.items[:limit]:
        lines.append(f"{t.tenure or 'Title'}: {t.address or t.title_number}"
                     + (f" (owner since {t.owner_since:%Y})" if t.owner_since else ""))
    return Page(url=DATASET_PAGE, kind="property", status_code=200,
                title="Property owned by the company (HM Land Registry)", text="\n".join(lines))
