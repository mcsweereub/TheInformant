"""Open roles from the company's public job board.

Applicant-tracking systems publish each employer's open roles through public JSON feeds
(the same feeds their careers-page widgets use). Supported: Greenhouse, Lever, Workable,
Ashby, SmartRecruiters and Recruitee. No key, and only job titles, departments, places,
dates and links are kept (never recruiters or other people).

The board is found from links or embed scripts on the company's own pages. Without one,
the brand name is tried on Greenhouse and Workable, whose feeds state the employer's
name, which must then match the company.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime

import httpx

from ..schema import Hiring, JobPosting, Source
from .companies_house.customers import name_key
from .fetchutil import api_client, cached_get

MAX_ROLES = 300

# (provider, regex finding the board token in a page's links or HTML)
BOARD_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([A-Za-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs(?:\.eu)?\.lever\.co/([A-Za-z0-9_.-]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/(?!api\b)([A-Za-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)", re.I)),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),
    ("recruitee", re.compile(r"https?://([a-z0-9-]+)\.recruitee\.com", re.I)),
]
_NOT_TOKENS = {"embed", "js", "api", "v1", "jobs", "j", "o", "static", "assets", "widget"}


@dataclass
class Board:
    provider: str
    token: str
    found_via: str  # "website" or "name"


def find_boards(page_html: list[str], links: list[str]) -> list[Board]:
    """Job boards linked or embedded in the company's pages, most mentioned first."""
    counts: Counter[tuple[str, str]] = Counter()
    for text in links + page_html:
        for provider, pattern in BOARD_PATTERNS:
            for token in pattern.findall(text):
                if token.lower() not in _NOT_TOKENS:
                    counts[(provider, token)] += 1
    return [Board(p, t, "website") for (p, t), _ in counts.most_common()]


def _date(value) -> date | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):  # milliseconds since 1970 (Lever)
            return datetime.fromtimestamp(value / 1000, tz=UTC).date()
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (ValueError, OSError):
        return None


def _place(*parts) -> str | None:
    return ", ".join(p for p in parts if p) or None


class JobFeeds:
    def __init__(self, cache=None, http: httpx.Client | None = None) -> None:
        self.cache = cache
        self.http = http or api_client()

    def _json(self, url: str, params: dict | None = None):
        return json.loads(cached_get(self.http, self.cache, url, params))

    def employer_name(self, board: Board) -> str | None:
        """The employer's name as the board states it (Greenhouse and Workable only)."""
        if board.provider == "greenhouse":
            return self._json(f"https://boards-api.greenhouse.io/v1/boards/{board.token}").get("name")
        if board.provider == "workable":
            return self._json(f"https://apply.workable.com/api/v1/widget/accounts/{board.token}").get("name")
        return None

    def roles(self, board: Board) -> tuple[str, list[JobPosting]]:
        """(public board URL, open roles)."""
        p, t = board.provider, board.token
        if p == "greenhouse":
            data = self._json(f"https://boards-api.greenhouse.io/v1/boards/{t}/departments")
            out = []
            for dept in data.get("departments") or []:
                for j in dept.get("jobs") or []:
                    out.append(JobPosting(title=j.get("title") or "", department=dept.get("name"),
                                          location=(j.get("location") or {}).get("name"),
                                          url=j.get("absolute_url"), posted_at=_date(j.get("updated_at"))))
            return f"https://job-boards.greenhouse.io/{t}", out
        if p == "lever":
            data = self._json(f"https://api.lever.co/v0/postings/{t}", {"mode": "json"})
            return f"https://jobs.lever.co/{t}", [
                JobPosting(title=j.get("text") or "", department=(j.get("categories") or {}).get("department")
                           or (j.get("categories") or {}).get("team"),
                           location=(j.get("categories") or {}).get("location"),
                           url=j.get("hostedUrl"), posted_at=_date(j.get("createdAt")))
                for j in data if isinstance(j, dict)]
        if p == "workable":
            data = self._json(f"https://apply.workable.com/api/v1/widget/accounts/{t}")
            return f"https://apply.workable.com/{t}", [
                JobPosting(title=j.get("title") or "", department=j.get("department") or None,
                           location=_place(j.get("city"), j.get("country")), url=j.get("url"),
                           posted_at=_date(j.get("published_on")))
                for j in data.get("jobs") or []]
        if p == "ashby":
            data = self._json(f"https://api.ashbyhq.com/posting-api/job-board/{t}")
            return f"https://jobs.ashbyhq.com/{t}", [
                JobPosting(title=j.get("title") or "", department=j.get("department") or j.get("team"),
                           location=j.get("location"), url=j.get("jobUrl"),
                           posted_at=_date(j.get("publishedAt")))
                for j in data.get("jobs") or []]
        if p == "smartrecruiters":
            data = self._json(f"https://api.smartrecruiters.com/v1/companies/{t}/postings")
            return f"https://careers.smartrecruiters.com/{t}", [
                JobPosting(title=j.get("name") or "", department=(j.get("department") or {}).get("label"),
                           location=_place((j.get("location") or {}).get("city"),
                                           (j.get("location") or {}).get("country", "").upper() or None),
                           url=f"https://jobs.smartrecruiters.com/{t}/{j['id']}" if j.get("id") else None,
                           posted_at=_date(j.get("releasedDate")))
                for j in data.get("content") or []]
        if p == "recruitee":
            data = self._json(f"https://{t}.recruitee.com/api/offers/")
            return f"https://{t}.recruitee.com", [
                JobPosting(title=j.get("title") or "", department=j.get("department") or None,
                           location=j.get("location") or None, url=j.get("careers_url"),
                           posted_at=_date(j.get("published_at")))
                for j in data.get("offers") or []]
        raise ValueError(f"unknown job board provider {p}")


def name_tokens(names: list[str]) -> list[str]:
    """Board tokens to try for a brand: 'monzo bank' -> 'monzobank', 'monzo-bank'."""
    out: list[str] = []
    for n in names:
        words = re.findall(r"[a-z0-9]+", n.lower())
        for token in ("".join(words), "-".join(words)):
            if len(token) >= 3 and token not in out:
                out.append(token)
    return out


def fetch_jobs(company_name: str, *, boards: list[Board], names: list[str], cache=None,
               http: httpx.Client | None = None) -> Hiring:
    """Open roles from the first board that answers. `boards` are those found on the
    site; `names` (brand and registered names) are tried on Greenhouse and Workable
    when the site links none."""
    own = http is None
    feeds = JobFeeds(cache=cache, http=http)
    section = Hiring()
    wanted = {name_key(n) for n in names + [company_name] if name_key(n)}
    candidates = list(boards)
    if not candidates:
        candidates = [Board(p, t, "name") for t in name_tokens(names) for p in ("greenhouse", "workable")]
    try:
        for board in candidates:
            try:
                if board.found_via == "name":
                    employer = feeds.employer_name(board)
                    if not employer or name_key(employer) not in wanted:
                        continue  # a different employer uses this name
                url, roles = feeds.roles(board)
            except httpx.HTTPStatusError:
                continue  # no such board
            except (httpx.HTTPError, ValueError) as e:
                section.notes.append(f"{board.provider} job board failed: {type(e).__name__}")
                continue
            seen: set[tuple] = set()  # Greenhouse lists a role under each of its departments
            unique = []
            for r in roles:
                key = (r.url,) if r.url else (r.title, r.location)
                if r.title and key not in seen:
                    seen.add(key)
                    unique.append(r)
            roles = unique
            if board.found_via == "name" and not roles:
                continue  # an empty board that only shares the name is weak evidence
            section.provider, section.board_url = board.provider, url
            section.found_via = board.found_via  # type: ignore[assignment]
            section.total = len(roles)
            section.departments = dict(Counter(r.department for r in roles if r.department).most_common(25))
            section.locations = dict(Counter(r.location for r in roles if r.location).most_common(25))
            roles.sort(key=lambda r: r.posted_at or date.min, reverse=True)
            for r in roles:
                r.sources = [Source(name="jobs", url=r.url or url)]
            section.open_roles = roles[:MAX_ROLES]
            section.sources = [Source(name="jobs", url=url)]
            if len(roles) > MAX_ROLES:
                section.notes.append(f"{len(roles)} open roles; the newest {MAX_ROLES} are listed")
            return section
        section.notes.append("no public job board found" if candidates else "no job board to check")
        return section
    finally:
        if own:
            feeds.http.close()


def jobs_material(section: Hiring):
    """Open roles as LLM material: what the company is building and where it's growing."""
    from .website.parse import Page

    if not section.open_roles:
        return None
    lines = [f"{section.total} open roles on its {section.provider} job board."]
    if section.departments:
        lines.append("By department: " + ", ".join(f"{d} ({n})" for d, n in section.departments.items()))
    if section.locations:
        lines.append("By location: " + ", ".join(f"{l} ({n})" for l, n in list(section.locations.items())[:10]))
    lines += [f"- {r.title}" + (f" ({r.department})" if r.department else "") for r in section.open_roles[:60]]
    return Page(url=section.board_url or "", kind="jobs", status_code=200,
                title="Open roles on the company's job board", text="\n".join(lines))
