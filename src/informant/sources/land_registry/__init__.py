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

from ...errors import ConfigError, UpstreamError
from .api import LandRegistryApi
from .common import (
    API,
    BATCH,
    CHECK_EVERY,
    DATASET,
    DATASET_PAGE,
    DATASETS,
    DB_NAME,
    MAX_ITEMS,
    OVERSEAS,
    OWNERS,
    TITLES,
    UK,
    Notify,
    api_key,
    data_dir,
    dataset_page,
    db_path,
    file_month,
    is_overseas_number,
    number_key,
)
from .countries import country_key, is_uk, same_country
from .lookup import (
    date_or_none,
    fetch_group_property,
    fetch_overseas_group_property,
    fetch_property,
    name_variants,
    property_material,
    source_for,
)
from .records import Row, owner_keys, parse_record, read_rows
from .store import PropertyStore, keep_current, update

__all__ = [
    "API", "BATCH", "CHECK_EVERY", "DATASET", "DATASET_PAGE", "DATASETS", "DB_NAME", "MAX_ITEMS",
    "OVERSEAS", "OWNERS", "TITLES", "UK", "ConfigError", "LandRegistryApi", "Notify",
    "PropertyStore", "Row", "UpstreamError", "api_key", "country_key", "data_dir", "dataset_page",
    "date_or_none", "db_path", "fetch_group_property", "fetch_overseas_group_property",
    "fetch_property", "file_month", "is_overseas_number", "is_uk", "keep_current", "name_variants",
    "number_key", "owner_keys", "parse_record", "property_material", "read_rows", "same_country",
    "source_for", "update",
]
