# TheInformant

Python CLI (uv, Python 3.12) that builds a JSON profile per UK company for
personal research. A separate, future program will visualise/categorise the output.

## Commands
- `uv run pytest`: tests (HTTP is mocked with respx, so no API key is needed)
- `uv run informant lookup <number|name>`: needs `COMPANIES_HOUSE_API_KEY` in `.env`
- `uv run informant model`: pick the LLM (Claude models or installed Ollama models); saved to `.env`
- The user prefers free tooling: default LLM is local Ollama (`qwen3:4b`: ~3 min per company on their CPU-only laptop; `gemma4:12b` timed out); Claude is opt-in.

## Layout
- `src/informant/schema.py`: output contract (pydantic). Bump `SCHEMA_VERSION` on breaking changes.
- `src/informant/companies_house.py`: API client and the mapping from raw JSON to the schema.
- `src/informant/discovery.py`: find the website (user > Wikidata > domain guess + verification).
- `src/informant/crawler.py`: polite crawler (robots.txt, delay, same host, key pages only).
- `src/informant/extract.py`: non-LLM extraction (contacts, socials, tech stack, ownership check).
- `src/informant/llm.py`: LLM extraction with two backends sharing one prompt/schema: Claude
  (`beta.messages.parse`) and Ollama (`/api/chat` with a JSON-schema `format`). Fixed SECTORS list.
- `src/informant/website.py`: website pipeline; merges results into CompanyProfile.
- `src/informant/pipeline.py`: `research_company()`: the whole lookup as one importable function,
  with a progress callback (`progress.py`). The CLI and any GUI both call this.
- `src/informant/storage.py`: atomic saves + per-folder `index.json`.
- `src/informant/errors.py`: error classes with stable `code` + `exit_code` (0/1/2/3/4).
- `src/informant/cli.py`: typer CLI. Every command supports `--json` (one JSON object on stdout,
  no prompts). Human messages go to stderr. This machine contract is public: keep it stable.

## Integration
- TheInformant must stay a standalone console app. A GUI (planned: React + local Python API
  + pywebview desktop window) will live in a separate project and use this one via the
  `--json` CLI contract or by importing `informant.pipeline`. Don't put GUI code here.

## Ground rules
- Company-focused. Store data about people **only** from official registries
  (Companies House officers/PSCs), and keep it minimal: no addresses, no DOBs.
  Never scrape LinkedIn or build profiles of individual employees.
- Prefer official APIs over scraping. When scraping: respect robots.txt and ToS,
  rate-limit, never bypass logins/paywalls/CAPTCHAs, and skip sites that block us.
- Emails: keep role addresses only (info@, sales@), not first.last@. Social links: company
  accounts only (LinkedIn /company/, never /in/).
- Don't send the user's personal details to outside services (e.g. no email in User-Agent);
  Wikidata needs contact info, so it runs only if the user sets INFORMANT_CONTACT.
- Every section/item records `sources` (name, url, retrieved_at).

## Roadmap (MVP order)
1. ~~Companies House: identity, SIC, address, officers, PSCs, filings~~
2. ~~Find the company website, crawl key pages~~
3. ~~LLM extraction of page text into the schema~~
4. Playwright fallback for JavaScript-rendered sites (e.g. monzo.com yields almost no text)
5. Recent news (Google News RSS / GNews)
6. Later: iXBRL financials, ATS job feeds (Greenhouse/Lever), SIC descriptions, batch mode, SQLite
