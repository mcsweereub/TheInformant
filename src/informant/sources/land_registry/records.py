"""Reading the dataset's CSV files into rows for the local database."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ...errors import UpstreamError
from ..website.extract import legal_key
from .common import (
    OWNERS,
    _date,
    _price,
    is_overseas_number,
    number_key,
)

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


def owner_keys(name: str, number: str, country: str = "") -> list[str]:
    """Lookup keys for one proprietor. UK companies: their number, or the exact name when
    the entry has none. Overseas companies: the exact name always (they are found by name
    from PSC and LEI records), plus a Companies House FC/OE number when there is one;
    numbers from foreign registers are not indexed."""
    if not country:
        key = f"n:{number_key(number)}" if number_key(number) else f"k:{legal_key(name)}"
        return [key] if len(key) > 2 else []
    keys = [f"k:{legal_key(name)}"] if legal_key(name) else []
    if is_overseas_number(number):
        keys.insert(0, f"n:{number_key(number)}")
    return keys


def parse_record(rec: dict) -> Row | None:
    title = (rec.get("Title Number") or "").strip()
    # the footer line is "Row Count:,<n>" (two cells, so Tenure holds the count)
    if not title or rec.get("Tenure") is None or title.lower().startswith("row count"):
        return None
    owners, keys = [], []
    for i in range(1, OWNERS + 1):
        name = (rec.get(f"Proprietor Name ({i})") or "").strip()
        if not name:
            continue
        number = (rec.get(f"Company Registration No. ({i})") or "").strip()
        address = ", ".join(a.strip() for a in (rec.get(f"Proprietor ({i}) Address ({j})")
                                                for j in (1, 2, 3)) if a and a.strip())
        country = (rec.get(f"Country Incorporated ({i})") or "").strip()  # overseas dataset only
        owners.append([name, number, (rec.get(f"Proprietorship Category ({i})") or "").strip(),
                       address, country])
        for key in owner_keys(name, number, country):
            if key not in keys:
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


