# TheInformant

Python CLI (uv, Python 3.12) that builds a JSON profile per UK company for
personal research. A separate, future program will visualise/categorise the output.

## Commands
- `uv run pytest`: tests (HTTP is mocked with respx, so no API key is needed)
- `uv run informant lookup <number|name>`: needs `COMPANIES_HOUSE_API_KEY` in `.env`
- `uv run informant model`: pick the LLM (Claude models or installed Ollama models); saved to `.env`
- The user prefers free tooling: default LLM is local Ollama (`qwen3:4b`: ~3-6 min per company on
  their CPU-only laptop; `gemma4:12b` timed out). Claude is opt-in; avoid API spend when testing.
- When smoke-testing real lookups, write to a scratch file (`--out-file`) so the user's saved
  profiles in `output/` aren't overwritten by `--no-llm` runs.

## Layout (src/informant/)
- `schema.py`: output contract (pydantic). Bump `SCHEMA_VERSION` on breaking changes.
- `pipeline.py`: `research_company()`: the whole lookup as one importable function, with a
  progress callback (`progress.py`). The CLI and any GUI both call this.
- `cli.py`: typer CLI. Every command supports `--json` (one JSON object on stdout, no prompts).
  Human messages go to stderr. This machine contract is public: keep it stable.
- `storage.py`: atomic saves + per-folder `index.json`. `errors.py`: stable `code` + `exit_code`.
- `sources/`: gathers raw material. `__init__` files import nothing (avoids import cycles).
  - `companies_house.py`: API client + mapping to the schema.
  - `website/discovery.py`: find the site (user > Wikidata > domain guess + verification).
  - `website/fetch.py`: `Crawler`: robots.txt, delays, same host; thin script-heavy pages are
    retried in a headless browser (`renderer.py`, Playwright). Discovery probes never render.
  - `website/pages.py`: which pages to read. PAGE_KINDS matched on URL path, then short labels
    (a label can't overrule a path naming another kind); LEGAL_RE pages skipped; /about
    fallback; spare slots filled from homepage menu links.
  - `website/parse.py`: Page/CrawlResult types, HTML -> text/links.
  - `website/extract.py`: non-LLM extraction (contacts, socials, tech stack, ownership check).
  - `website/research.py`: website stage (discover -> crawl -> extract -> LLM -> merge).
- `llm/`: raw text -> structured fields. Public API re-exported from `llm/__init__.py`.
  - `models.py` backend/model choice; `prompts.py` prompt + SECTORS + output schema (shared by
    all backends); `budget.py` repeated-line removal + fair share per page; `result.py` checks
    on every answer (echoed-prompt guard); `backends/claude.py` (`beta.messages.parse`) and
    `backends/ollama.py` (`/api/chat` + JSON-schema `format`), both exposing
    `complete(system, user, schema)`; `staged.py` notes-per-part + merge (products/sites are
    combined in code, the merge call only writes summary/sectors/tags); `extract.py` picks
    backend + strategy (`--llm-strategy`: auto = single for Claude, staged for Ollama when the
    de-duplicated text exceeds ~12k chars).

## Integration
- TheInformant must stay a standalone console app. A GUI (planned: React + local Python API
  + pywebview desktop window) will live in a separate project and use this one via the
  `--json` CLI contract or by importing `informant.pipeline`. Don't put GUI code here.

## Gotchas
- Don't write Python source (or this file) through bash heredocs containing backslash escapes
  such as backslash-b or backslash-n: they have turned into real control characters and line
  breaks here. Use the Write/Edit tools.

## Ground rules
- Company-focused. Store data about people **only** from official registries
  (Companies House officers/PSCs), and keep it minimal: no addresses, no DOBs.
  Never scrape LinkedIn or build profiles of individual employees.
- Prefer official APIs over scraping. When scraping: respect robots.txt and ToS,
  rate-limit, never bypass logins/paywalls/CAPTCHAs, and skip sites that block us.
  Don't scrape Google results pages: use an official search API instead.
- Emails: keep role addresses only (info@, sales@), not first.last@. Social links: company
  accounts only (LinkedIn /company/, never /in/).
- Don't send the user's personal details to outside services (e.g. no email in User-Agent).
  Wikimedia (Wikidata, Wikipedia) requires contact info in the User-Agent, so those sources run
  only if the user sets INFORMANT_CONTACT (they've agreed to set one for Wikipedia).
- Every section/item records `sources` (name, url, retrieved_at).

## Roadmap
Done: Companies House; website discovery + crawl; LLM extraction; Playwright fallback;
restructure into `sources/` and `llm/` packages; staged LLM pipeline (`--llm-strategy`).
New sources should feed the staged pipeline as extra material (pages/documents), not add
their own LLM plumbing.
2. Sources, one at a time: news (Google News RSS + optional Guardian key), Wikipedia (official
   API, needs INFORMANT_CONTACT), web search (official API with free tier, e.g. Brave).
3. Customers: extract named client companies into the profile; resolve UK ones to Companies
   House numbers; optional `--with-customers` deep dive; customer links in the index.
4. Later: iXBRL financials, ATS job feeds (Greenhouse/Lever), SIC descriptions, batch mode, SQLite.
