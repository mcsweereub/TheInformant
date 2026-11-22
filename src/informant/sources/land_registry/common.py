"""Constants, file names and small parsers shared by the Land Registry modules."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

API = "https://use-land-property-data.service.gov.uk/api/v1"
UK = "ccod"  # UK companies that own property in England and Wales
OVERSEAS = "ocod"  # overseas companies that own property in England and Wales
DATASETS = (UK, OVERSEAS)
TITLES = {UK: "UK companies that own property in England and Wales",
          OVERSEAS: "Overseas companies that own property in England and Wales"}
DATASET = UK
DATASET_PAGE = "https://use-land-property-data.service.gov.uk/datasets/ccod"
DB_NAME = "land_registry_ccod.sqlite"
MAX_ITEMS = 250  # titles listed per company (totals always cover all of them)
CHECK_EVERY = timedelta(days=1)  # how often a lookup asks the API for a newer month
BATCH = 50_000
OWNERS = 4  # the CSV has up to four proprietors per title
_FILE_RE = re.compile(r"^(CCOD|OCOD)_(FULL|COU)_(\d{4})_(\d{2})\.zip$", re.I)
# Companies House numbers given to overseas companies (FC/SF/NF: UK establishments,
# OE: the register of overseas entities). Other numbers in the overseas dataset come from
# foreign registers and are never treated as Companies House numbers.
_UK_OVERSEAS_NUMBER = re.compile(r"^(FC|SF|NF|OE)\d{4,6}$", re.I)

Notify = Callable[[str], None]


def data_dir() -> Path:
    """Downloaded datasets (not a cache: `informant cache --clear` leaves them alone)."""
    return Path(os.environ.get("INFORMANT_DATA_DIR") or ".informant_data")


def dataset_page(dataset: str = UK) -> str:
    return f"https://use-land-property-data.service.gov.uk/datasets/{dataset}"


def db_path(dataset: str = UK) -> Path:
    return data_dir() / f"land_registry_{dataset}.sqlite"


def is_overseas_number(number: str | None) -> bool:
    """A Companies House number for an overseas company (FC012345, OE001234, ...)."""
    return bool(_UK_OVERSEAS_NUMBER.match((number or "").strip().replace(" ", "")))


def api_key() -> str | None:
    return (os.environ.get("LAND_REGISTRY_API_KEY") or "").strip() or None


def file_month(file_name: str) -> str | None:
    """'CCOD_COU_2026_11.zip' -> '2026-11'."""
    m = _FILE_RE.match(file_name or "")
    return f"{m.group(3)}-{m.group(4)}" if m else None


def _file_kind(file_name: str, dataset: str) -> str | None:
    """'FULL' / 'COU' for one of the dataset's files, else None."""
    m = _FILE_RE.match(file_name or "")
    return m.group(2).upper() if m and m.group(1).lower() == dataset else None


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


