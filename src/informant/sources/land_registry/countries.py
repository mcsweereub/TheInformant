"""Countries of incorporation: one spelling per country, so the overseas dataset's
"U.S.A." matches a PSC record's "United States" and an LEI record's "US"."""

from __future__ import annotations

import re

# --- countries (the overseas dataset names them; PSC and LEI records vary) ---------------

_COUNTRY_ALIASES = {
    "JE": "JERSEY", "GG": "GUERNSEY", "IM": "ISLE OF MAN", "VG": "BRITISH VIRGIN ISLANDS",
    "BVI": "BRITISH VIRGIN ISLANDS", "VIRGIN ISLANDS BRITISH": "BRITISH VIRGIN ISLANDS",
    "VIRGIN ISLANDS": "BRITISH VIRGIN ISLANDS",
    "KY": "CAYMAN ISLANDS", "CAYMAN": "CAYMAN ISLANDS", "LU": "LUXEMBOURG",
    "NL": "NETHERLANDS", "THE NETHERLANDS": "NETHERLANDS", "HOLLAND": "NETHERLANDS",
    "IE": "IRELAND", "REPUBLIC OF IRELAND": "IRELAND", "SG": "SINGAPORE", "HK": "HONG KONG",
    "GI": "GIBRALTAR", "AE": "UNITED ARAB EMIRATES", "UAE": "UNITED ARAB EMIRATES",
    "DUBAI": "UNITED ARAB EMIRATES", "ABU DHABI": "UNITED ARAB EMIRATES", "BM": "BERMUDA",
    "CH": "SWITZERLAND", "DE": "GERMANY", "FR": "FRANCE", "CY": "CYPRUS", "MT": "MALTA",
    "LI": "LIECHTENSTEIN", "BS": "BAHAMAS", "PA": "PANAMA", "CA": "CANADA", "AU": "AUSTRALIA",
    "QA": "QATAR", "SA": "SAUDI ARABIA", "KW": "KUWAIT", "IN": "INDIA", "CN": "CHINA",
    "JP": "JAPAN", "SE": "SWEDEN", "NO": "NORWAY", "DK": "DENMARK", "BE": "BELGIUM",
    "ES": "SPAIN", "IT": "ITALY",
    "US": "USA", "USA": "USA", "UNITED STATES": "USA", "UNITED STATES OF AMERICA": "USA",
    "DELAWARE": "USA", "NEW YORK": "USA", "CALIFORNIA": "USA", "NEVADA": "USA",
    "GB": "UNITED KINGDOM", "UK": "UNITED KINGDOM", "ENGLAND": "UNITED KINGDOM",
    "WALES": "UNITED KINGDOM", "SCOTLAND": "UNITED KINGDOM", "NORTHERN IRELAND": "UNITED KINGDOM",
    "ENGLAND AND WALES": "UNITED KINGDOM", "ENGLAND WALES": "UNITED KINGDOM",
    "GREAT BRITAIN": "UNITED KINGDOM", "UNITED KINGDOM": "UNITED KINGDOM",
}


def _words(text: str) -> str:
    return " ".join(re.sub(r"[^A-Z& ]+", " ", text.upper().replace(".", "")).replace("&", "AND").split())


def country_key(text: str | None) -> str | None:
    """One spelling per country: 'U.S.A.', 'United States' and 'Delaware, USA' -> 'USA';
    'JE' (an LEI code) and 'Jersey' -> 'JERSEY'. Unknown names are just upper-cased."""
    whole = _words(text or "")
    if not whole:
        return None
    if whole in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[whole]
    for part in reversed(re.split(r"[,(/]", text or "")):  # "Delaware, United States"
        key = _words(part)
        if key in _COUNTRY_ALIASES:
            return _COUNTRY_ALIASES[key]
    return whole


def is_uk(country: str | None) -> bool:
    return country_key(country) == "UNITED KINGDOM"


def same_country(a: str | None, b: str | None) -> bool | None:
    """True / False, or None when either is unknown."""
    ka, kb = country_key(a), country_key(b)
    if not ka or not kb:
        return None
    return ka == kb or ka in kb or kb in ka


