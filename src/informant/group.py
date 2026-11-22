"""The company's group, worked out from its profile: UK parents and subsidiaries (by
Companies House number) and overseas ones (by name and country), from the PSC chain
and the LEI record. Used to search donations and property across the group."""

from __future__ import annotations

from .schema import CompanyProfile
from .sources.land_registry import is_overseas_number, is_uk
from .sources.website.extract import legal_key


def group_companies(profile: CompanyProfile) -> list[tuple[str, str, str]]:
    """Other UK-registered companies in the group as (number, name, relation): parents from
    the PSC chain and the LEI record, then subsidiaries that have LEIs."""
    own = (profile.identity.company_number or "").lstrip("0")
    out: dict[str, tuple[str, str, str]] = {}

    def add(number: str | None, name: str, relation: str) -> None:
        key = (number or "").upper().lstrip("0")
        if key and key != own and key not in out:
            out[key] = (number, name, relation)

    for parent in profile.ownership.parents:
        add(parent.company_number, parent.name, "parent")
    lei = profile.lei
    for parent in (lei.direct_parent, lei.ultimate_parent):
        if parent is not None:
            add(parent.company_number, parent.name, "parent")
    for child in lei.subsidiaries:
        add(child.company_number, child.name, "subsidiary")
    return list(out.values())


MAX_OVERSEAS_GROUP = 12


def overseas_group_companies(profile: CompanyProfile) -> list[tuple[str | None, str, str | None, str]]:
    """Group companies incorporated outside the UK, as (FC/OE number or None, name, country,
    relation): corporate owners in the PSC chain registered abroad, and LEI parents and
    subsidiaries whose legal address is outside the UK. Entries of unknown country are
    left out (they can't be told apart from UK companies)."""
    own = legal_key(profile.identity.name)
    out: dict[str, tuple[str | None, str, str | None, str]] = {}

    def add(number: str | None, name: str, country: str | None, relation: str) -> None:
        key = legal_key(name)
        overseas = is_overseas_number(number) or (not number and country and not is_uk(country))
        if key and key != own and overseas and key not in out:
            out[key] = (number if is_overseas_number(number) else None, name, country, relation)

    for parent in profile.ownership.parents:
        add(parent.company_number, parent.name, parent.country_registered, "parent")
    lei = profile.lei
    for parent in (lei.direct_parent, lei.ultimate_parent):
        if parent is not None:
            add(parent.company_number, parent.name, parent.country, "parent")
    for child in lei.subsidiaries:
        add(child.company_number, child.name, child.country, "subsidiary")
    return list(out.values())[:MAX_OVERSEAS_GROUP]
