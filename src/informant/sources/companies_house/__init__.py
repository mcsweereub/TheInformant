"""Companies House as a source: the official UK company register (free API).

    client.py     API client, errors, company-number handling, filed documents
    mapping.py    core data: identity, SIC codes, address, officers, PSCs, filings
    accounts.py   figures from filed accounts (iXBRL): turnover, net assets, employees...
    capital.py    share capital filings (allotments = funding signal) and charges (lenders)
    ownership.py  corporate owners traced up to the ultimate parent
    related.py    other companies sharing current officers
    risk.py       flags computed from all of the above
    details.py    runs the steps above and fills the profile

Imports only its own submodules and the schema (no dependency on the llm package).
"""

from .client import (
    BASE_URL,
    CompaniesHouseAuthError,
    CompaniesHouseClient,
    CompaniesHouseError,
    CompanyNotFoundError,
    SearchHit,
    normalise_company_number,
)
from .mapping import CompaniesHouseData, RawCompany, fetch_company, map_companies_house

__all__ = [
    "BASE_URL", "CompaniesHouseAuthError", "CompaniesHouseClient", "CompaniesHouseData",
    "CompaniesHouseError", "CompanyNotFoundError", "RawCompany", "SearchHit", "fetch_company",
    "map_companies_house", "normalise_company_number",
]
