"""Matching named customers to the Companies House register.

A customer named on a website ("Centrica", "BP") is searched for on the register.
  exact   the registered name, without legal suffixes, is the same as the name given
          ("CENTRICA PLC" for "Centrica"); an active company is preferred
  likely  no exact match, but one result starts with the name given and no other does
  none    nothing suitable: often a company registered abroad (Gunvor, PetroChina)

Only the register's own data is used (number, name, status); nothing is guessed.
"""

from __future__ import annotations

import re

from ...schema import Customer
from ..website.extract import core_name
from .client import CompaniesHouseClient, CompaniesHouseError

MAX_LOOKUPS = 25  # register searches per profile


def _key(name: str) -> str:
    """'B.P. P.L.C.' and 'BP plc' -> 'bp'; 'Marks & Spencer' -> 'marks and spencer'."""
    cleaned = re.sub(r"(?<=\b\w)\.(?=\w\.)|(?<=\b\w)\.(?=\s|$)", "", name)  # B.P. -> BP
    cleaned = cleaned.replace("&", " and ").replace(".", " ")
    return core_name(cleaned)


def match_customer(client: CompaniesHouseClient, customer: Customer) -> None:
    wanted = _key(customer.name)
    if len(wanted) < 2:
        customer.match = "none"
        return
    hits = client.search(customer.name, limit=10)
    exact = [h for h in hits if _key(h.title) == wanted]
    if exact:
        best = next((h for h in exact if (h.status or "") == "active"), exact[0])
        customer.match = "exact"
    else:
        starts = [h for h in hits if _key(h.title).startswith(wanted + " ")]
        if len(starts) != 1:
            customer.match = "none"
            return
        best = starts[0]
        customer.match = "likely"
    customer.company_number = best.company_number
    customer.registered_name = best.title
    customer.status = best.status


def resolve_customers(client: CompaniesHouseClient, customers: list[Customer],
                      own_number: str | None = None) -> list[str]:
    """Match each customer in place; returns notes. A customer that turns out to be the
    company itself (same number) is removed."""
    notes: list[str] = []
    for customer in customers[:MAX_LOOKUPS]:
        try:
            match_customer(client, customer)
        except CompaniesHouseError as e:
            notes.append(f"could not look up customer {customer.name}: {e}")
            break  # a register problem will affect every lookup; stop
    if len(customers) > MAX_LOOKUPS:
        notes.append(f"matched the first {MAX_LOOKUPS} of {len(customers)} customers on the register")
    if own_number:
        customers[:] = [c for c in customers if c.company_number != own_number]
    return notes
