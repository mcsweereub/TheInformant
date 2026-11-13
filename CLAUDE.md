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
- Real-world test companies (the user wants quick tests, not 20+ minute runs):
  - Planlogic Software (09461711, planlogic.com): the standard end-to-end test, ~3-4 min.
    Covers iXBRL accounts, WordPress, JSON-LD, a menu-only product page (/eos/), a case-studies
    page, the /about-us fallback, a mislabelled charset, and qwen's "lpg" tag invention.
  - Monzo Bank (09446231) with `--no-web`: news (many articles, ~2 min), corporate owner,
    charges, share allotments. Its full website run is the large JavaScript-site case
    (headless browser + staged extraction) and is slow; only run it when that path changed.
  - Greggs (00502851) with `--no-llm` / `--no-web`: PDF-only accounts, PLC deadlines.

## Layout (src/informant/)
- `schema.py`: output contract (pydantic). Bump `SCHEMA_VERSION` on breaking changes.
- `pipeline.py`: `research_company()`: the whole lookup as one importable function, with a
  progress callback (`progress.py`). The CLI and any GUI both call this.
- `cli.py`: typer CLI. Every command supports `--json` (one JSON object on stdout, no prompts).
  Human messages go to stderr. This machine contract is public: keep it stable.
- `storage.py`: atomic saves + per-folder `index.json`. `errors.py`: stable `code` + `exit_code`.
- `cache.py`: `.informant_cache/` with crawled pages (7-day TTL) and LLM answers (keyed by
  model + prompts + schema; never expire). `--refresh`, `--no-cache`, `informant cache`.
  Tests run with the cache off (`tests/conftest.py`); cache tests opt in with a temp dir.
  Only reuse a cached page if rendering couldn't improve it (probes cache unrendered shells).
- `sources/`: gathers raw material. `__init__` files import nothing (avoids import cycles).
  - `companies_house/`: `client.py` (API + filed documents; the document redirect to storage
    must be followed WITHOUT the API key), `mapping.py` (core data; keeps `RawCompany` so later
    steps don't refetch), `accounts.py` (iXBRL figures; match tag local names, ignore
    dimensioned contexts; PDF-only filings are noted, never guessed), `capital.py` (share
    capital events + charges), `ownership.py` (PSC chain), `related.py` (shared officers),
    `risk.py` (rule-based flags with evidence), `details.py` (runs them; one failing step
    leaves a note and the rest continue). `--registry full|basic`.
    Known API quirks: profile `has_charges` is unreliable (always call /charges); filing dates
    can lag receipt and year ends float +/-7 days (late-filing flag has a 14-day tolerance).
  - `website/discovery.py`: find the site (user > Wikidata > domain guess + verification).
  - `website/fetch.py`: `Crawler`: robots.txt, delays, same host; thin script-heavy pages are
    retried in a headless browser (`renderer.py`, Playwright). Discovery probes never render.
  - `website/pages.py`: which pages to read. PAGE_KINDS matched on URL path, then short labels
    (a label can't overrule a path naming another kind); LEGAL_RE pages skipped; kinds the
    homepage doesn't link are looked up in the sitemap (robots.txt `Sitemap:` or /sitemap.xml;
    INFORMANT_SITEMAP=off, and off in older tests via conftest); /about fallback; spare slots
    filled from homepage menu links. robots.txt is cached for 1 day, missing sitemaps too.
    Page bytes are decoded as UTF-8 when valid (servers often mislabel the charset).
  - `website/parse.py`: Page/CrawlResult types, HTML -> text/links.
  - `website/extract.py`: non-LLM extraction (contacts, socials, tech stack, ownership check).
  - `website/structured.py`: JSON-LD / OpenGraph facts (organisation-level only, never the
    people in the markup) -> `website.structured`, contacts, an office location, and a
    "structured" fact-sheet page placed first in the LLM material.
  - `website/research.py`: website stage (discover -> crawl -> extract -> LLM -> merge).
- `llm/`: raw text -> structured fields. Public API re-exported from `llm/__init__.py`.
  - `models.py` backend/model choice; `prompts.py` prompt + SECTORS + output schema (shared by
    all backends; sectors and business model are enums in the schema, with tolerant
    validators that drop unknown values instead of failing); `budget.py` repeated-line removal
    + fair share per page; `result.py` checks on every answer: echoed-prompt guard, grounding
    (tags/products/sites must appear in the source text, with simple stemming), duplicate-site
    merge, and a tag top-up from frequent page terms when fewer than 3 survive (qwen3:4b
    reliably invents "lpg" tags for an LNG site; prompt wording didn't fix it, the check does);
    `backends/claude.py` (`beta.messages.parse`) and
    `backends/ollama.py` (`/api/chat` + JSON-schema `format`), both exposing
    `complete(system, user, schema)`; `staged.py` notes-per-part + merge (products/sites are
    combined in code, the merge call only writes summary/sectors/tags); `extract.py` picks
    backend + strategy (`--llm-strategy`: auto = single for Claude, staged for Ollama when the
    de-duplicated text exceeds ~12k chars); `make_complete()` gives any feature the configured
    backend + cache. Staged defaults: 3 parts x 7,000 chars (was 5 x 9,000; Monzo took 17-27
    min that way and later parts added little).
  - `news.py`: one call judges all headlines (relevance, sentiment, topic) from fixed lists.
- `sources/news.py`: Bing News RSS (one query per name; the feed ignores OR) + GDELT DOC API
  (1 request / 5 s, often 429s; best-effort) + Guardian API if GUARDIAN_API_KEY. Google News
  RSS is disallowed by its robots.txt: don't use it. Headlines must name the company
  (`mentions`); names = domain brand, registered core name, distinctive first word.
  `pipeline.research_news()` fills `profile.news`; doubtful headlines are kept with
  relevant=False (qwen dropped real "Nubank not pursuing Monzo" stories) and hidden in the CLI.
  qwen3:4b rates nearly all headlines neutral: sentiment from it is weak.

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
restructure into `sources/` and `llm/` packages; staged LLM pipeline (`--llm-strategy`);
local cache. Measured on the user's laptop (qwen3:4b): ~4-5 min per LLM call; Monzo staged
(5 parts + merge) took 27 min and completed; small sites ~4 min single-call.
Also done: Companies House depth (accounts, capital, charges, ownership, related, risk flags).
Tests: older tests run with INFORMANT_REGISTRY=basic (conftest); full depth is tested in
`tests/test_registry_details.py`.
Also done: answer checks (schema enums, grounding, output cap `num_predict`), structured data
(JSON-LD/OpenGraph), sitemap page discovery, news (Bing + GDELT + optional Guardian).

Agreed next, in this order (the user liked all of these):
2. Sources, one at a time: Wikipedia (official API, needs INFORMANT_CONTACT), then web search
   (official API with free tier, e.g. Brave). New sources feed the staged pipeline as extra
   material; no separate LLM plumbing. Tag quality: prefer tags from structured data /
   frequent terms over the small model's free choice (it invents "lpg" for LNG).
3. Customers: extract named client companies into the profile; resolve UK ones to Companies
   House numbers; optional `--with-customers` deep dive; customer links in the index.
4. More legal public data the user approved: Companies House bulk downloads (offline
   screening), WordPress `/wp-json` and Shopify `/products.json` feeds, shared analytics IDs
   (sister sites), DNS/certificate-transparency signals, ad transparency libraries, PDFs via
   search API, Land Registry CCOD, Contracts Finder, GLEIF, Gazette, ATS job feeds, Wayback.
   For large companies with PDF-only accounts: text extraction + LLM is a possible later step.
5. Longer term: facts store in SQLite with provenance, change tracking, relationship graph.
6. Last, as an explicit opt-in the user asked to explore at the end: a `--user-agent` option,
   then maybe review/job sites (Trustpilot, Glassdoor, Indeed; these break site terms).
The user agreed the stop points: no bypassing logins/paywalls/CAPTCHAs, and no profiles of
individual people (no employee lists, LinkedIn profiles or inferred emails).
