"""Related companies: other companies that share current officers with this one.

Directors often run several companies (sister companies, holding companies,
earlier ventures). Each officer's appointments list is public registry data;
this turns those lists into a company-to-company view.
"""

from __future__ import annotations

from ...schema import RelatedCompanies, RelatedCompany
from .client import CompaniesHouseClient, CompaniesHouseError
from .mapping import source

MAX_OFFICERS = 20
MAX_FETCHED = 100  # appointments read per officer
MAX_APPOINTMENTS = 30  # more current appointments than this: probably a professional or nominee
MAX_RELATED = 40


def fetch_related(client: CompaniesHouseClient, number: str,
                  officers: list[dict]) -> RelatedCompanies:
    result = RelatedCompanies(sources=[source(number, "/officers")])
    active = [o for o in officers if not o.get("resigned_on")]
    # Directors and members first: secretaries are more often service providers.
    active.sort(key=lambda o: "secretary" in (o.get("officer_role") or ""))
    if len(active) > MAX_OFFICERS:
        result.notes.append(f"checked the first {MAX_OFFICERS} of {len(active)} current officers")

    found: dict[str, RelatedCompany] = {}
    for officer in active[:MAX_OFFICERS]:
        path = ((officer.get("links") or {}).get("officer") or {}).get("appointments")
        name = officer.get("name", "")
        if not path:
            continue
        try:
            items, total = client.appointments(path, limit=MAX_FETCHED)
        except CompaniesHouseError as e:
            result.notes.append(f"could not read appointments for {name}: {e}")
            continue
        current = [a for a in items if not a.get("resigned_on")]
        if total > MAX_FETCHED or len(current) > MAX_APPOINTMENTS:
            count = total if total > MAX_FETCHED else len(current)
            result.notes.append(
                f"{name} holds {count} appointments (probably a professional or nominee "
                "officer); not followed")
            continue
        for appt in current:
            company = appt.get("appointed_to") or {}
            other = company.get("company_number")
            if not other or other == number:
                continue
            related = found.setdefault(other, RelatedCompany(
                company_number=other, name=company.get("company_name", ""),
                status=company.get("company_status")))
            if name not in related.shared_officers:
                related.shared_officers.append(name)
            role = appt.get("officer_role")
            if role and role not in related.roles:
                related.roles.append(role)

    ranked = sorted(found.values(), key=lambda r: (-len(r.shared_officers), r.name))
    if len(ranked) > MAX_RELATED:
        result.notes.append(f"showing {MAX_RELATED} of {len(ranked)} related companies")
    result.companies = ranked[:MAX_RELATED]
    return result
