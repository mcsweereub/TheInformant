# TheInformant

Researches UK companies and writes one structured JSON profile per company,
ready for a separate visualisation/categorisation program to consume.

## Setup

```sh
uv sync
cp .env.example .env   # then paste your Companies House API key into .env
uv run playwright install chromium   # optional (~150 MB): reads JavaScript-built websites
winget install UB-Mannheim.TesseractOCR   # optional: OCR for scanned (PDF-only) accounts
```

Get a free key at https://developer.company-information.service.gov.uk/
(register, create an application, add a **REST** API key).

## Usage

```sh
uv run informant search "greggs"          # list matches
uv run informant lookup 00502851          # by company number
uv run informant lookup "greggs plc"      # by name (uses the top match)
uv run informant lookup 09446231 -w monzo.com   # tell it the website
uv run informant lookup 00502851 --no-llm # skip Claude extraction
uv run informant lookup 00502851 --no-web # Companies House only
uv run informant schema > schema.json     # JSON Schema for downstream programs
uv run informant model                    # pick the LLM from a numbered list
```

## Choosing the LLM

`uv run informant model` lists the Claude models and every model installed in
Ollama, then saves your pick to `.env`. You can also pass a choice directly:
`uv run informant model 3` or `uv run informant model gemma4:12b`.

- **Ollama (free).** Runs a local model through [Ollama](https://ollama.com). Install a
  model with `ollama pull <name>`. Local models are slower (minutes per company on a CPU)
  and less accurate than Claude, but cost nothing.
- **Claude (paid).** Needs `ANTHROPIC_API_KEY` in `.env`.

Each profile records which model filled its LLM fields in `website.llm_model`.

Profiles are written to `output/<number>-<name>.json`.

## What comes from Companies House

All of this is exact registry data: no LLM, no scraping, about 4 seconds per company.

- **Core**: name, status, SIC codes, registered office, officers, persons with
  significant control, recent filings, whether accounts are overdue.
- **Filed accounts** (`financials`): turnover, profit, net assets, cash and employees
  for the last 3-4 years, read from the figures tagged in the filed accounts (iXBRL).
  Small companies often file no profit and loss account, so turnover may be absent.
- **PDF-only accounts** (large companies, groups, banks): Companies House stores these
  as scanned images. The tool then reads the figures (turnover, profits, total and net
  assets, cash, employees), principal activities, auditor, ultimate controlling party and
  substantial shareholders from either:
  1. the company's own annual report, a text PDF found through its website, investor
     or corporate site, sitemap or Wikipedia references, and accepted only if it names
     the company and the exact year-end date; or
  2. an OCR of the filed scan (needs Tesseract): a quick pass over every page finds the
     statements, which are then read at full detail.

  The LLM copies figures exactly as printed; code converts the units and drops any
  figure that doesn't appear on the pages. Each year records how it was read
  (`method`: `ixbrl`, `annual_report` or `ocr`); OCR can misread a digit, so check
  important OCR figures against the document. A large report takes a few minutes to
  find and read (Monzo's is 59 MB), plus two LLM calls. `--no-pdf-accounts` skips it.
- **Share capital** (`capital`): allotments, buybacks and cancellations. An allotment
  means new shares were issued, usually a funding round or a share scheme.
- **Charges** (`charges`): mortgages and other security, with the lenders' names and
  whether each is still outstanding.
- **Ownership** (`ownership`): corporate owners traced upward to the top of the chain,
  and the people registered as controlling it.
- **Related companies** (`related_companies`): other companies that share current
  officers (sister companies, holding companies, earlier ventures).
- **Risk flags** (`risk`): rules over the facts above, each with its evidence.
  `high` = formally wrong (overdue accounts, insolvency, strike-off notice);
  `warning` = a pattern worth a closer look (several directors leaving, late accounts,
  shrinking balance sheet); `info` = worth knowing (new shares issued, headcount change).
  A flag is a prompt to look closer, not a verdict.

`--registry basic` keeps just the core (and the flags that need only the core).

## How the website stage works

1. **Find the site.** Uses `--website` if given. Otherwise it tries Wikidata (only
   when `INFORMANT_CONTACT` is set), then guesses domains from the company name.
   A guessed site is kept only if it shows the company number or registered
   postcode (UK companies must show their number on their website), or has the
   name in its page title.
2. **Crawl politely.** Fetches the homepage plus up to 7 key pages: about, products,
   investors, customers (case studies), careers, contact and news. Pages are matched by
   address first, then by short menu labels; legal pages (terms, privacy, cookies,
   regulatory notices) are skipped. Page kinds the homepage doesn't link are looked up in
   the site's sitemap (`INFORMANT_SITEMAP=off` disables this). Spare slots go to the
   homepage's other menu links, so a product page like `/eos/` is still read. It obeys
   robots.txt, waits 1s
   between requests, stays on the same site, and skips anything that blocks it.
   If a page comes back nearly empty because JavaScript builds it (e.g. monzo.com),
   it reloads that page in a headless Chromium browser, and loads the rest of that
   site the same way. Control this with `--browser auto|always|never` (default auto).
3. **Extract without an LLM.** Role email addresses (info@, sales@...),
   phone numbers, company social accounts and tech stack. Also the facts the site
   declares about itself in machine-readable form (schema.org JSON-LD and OpenGraph
   tags): official name, description, logo, founding date, employee range, address.
   These go into `website.structured`, and are handed to the LLM as a fact sheet.
4. **Extract with an LLM** (Claude or a local Ollama model). Summary, sectors from a
   fixed list, tags, business model, target markets, products and offices.
   Every answer is checked before it's kept:
   - Sectors and business model are fixed lists in the output format itself, so a
     model can't invent a value.
   - Tags, products and sites must be supported by the page text; unsupported ones are
     dropped, with a note saying which. If fewer than three tags survive, the gap is
     filled with terms the pages themselves repeat.
   - An answer that copies the instructions is discarded.
   `--llm-strategy` controls how the text is sent:
   - `single`: everything in one call.
   - `staged`: the text is read in parts (about 9,000 characters each, up to 5 parts),
     each part yields notes, and a final call merges the notes into the profile. Local
     models are slow with long inputs, so this lets them finish large sites. A failed
     part is skipped, and each call has its own 20-minute limit. Every page still gets
     a fair share of the space. `INFORMANT_STAGE_CHARS` and `INFORMANT_MAX_STAGES`
     change the sizes (more parts read more of a big site, but take longer).
   - `auto` (default): one call for Claude; for Ollama, one call when the text fits
     (about 12,000 characters), otherwise staged.

Anything skipped or uncertain is recorded in `website.notes`.

## Wikipedia and Wikidata

Runs when `INFORMANT_CONTACT` is set (Wikimedia requires contact details in the
User-Agent; the value is sent to Wikimedia only). `--no-wikipedia` skips it.

- **Finding the entry**: Wikidata records Companies House numbers for many companies, so
  the lookup tries the exact number first. Otherwise it searches Wikipedia by name and
  accepts a result only if it is a UK entity with a matching name and no different
  company number.
- **Facts** (`wikipedia`): short description, the article's lead, founding year,
  industries, headquarters, parent organisation, stock listings with tickers, employee
  count, official websites. Founders and other people are not read.
- The whole article (history, operations, products, fundraising...) is handed to the LLM
  with the website's pages, minus sections about people (leadership, boards, lists of
  chairmen), which aren't recorded. If the company has no website, or you pass
  `--no-web`, the LLM still runs on the article. Wikidata's industries are added to the
  tags. Without an LLM, the description comes from the article's first sentences.
- **References** (`wikipedia.references`): every source the article cites, with the
  citation text and link, for further reading.

## History, funding and key facts

The LLM also records, from any source (website pages or Wikipedia):

- `history`: dated milestones (founding, launches, new markets, acquisitions, listings,
  rebrands, closures, legal events), oldest first.
- `funding.rounds`: funding rounds with year, kind (seed, Series A, IPO...), amount as
  written, and the investing organisations.
- `funding.shareholders`: organisations reported to hold shares, with the stake if given.
  Individual people aren't recorded; officially registered controllers are in `ownership`.
- `key_facts`: short, self-contained statements about the company.

Every entry is checked against the source text: a milestone's year and most of its words
must appear there, investors and shareholders must be named there, and duplicates are
merged. Each entry records the page it came from.

## Customers

Every lookup lists the customers the material names (`customers`): organisations in case
studies, client lists, "trusted by" sections or contract announcements, with a few words on
how each is mentioned and the page it came from. Only organisations are recorded; names
must appear in the text; generic phrases and the company itself are dropped.

Each customer is then looked up on Companies House: UK companies get their number, registered
name and status (`match`: `exact`, `likely` or `none`; foreign companies stay unmatched).

Researching the customers themselves is optional, now or later. Each becomes its own
profile in the same folder, with the same options as the main lookup (customers' own
customers are listed but not researched further):

```sh
uv run informant lookup 09461711 --with-customers 5   # research up to 5 customers now
uv run informant customers 09461711                   # later: list a saved company's customers
uv run informant customers 09461711 --research -n 5   # later: research them
```

`index.json` links them both ways: each entry lists its `customers`, and `customer_of`
names the saved companies that list it as a customer.

## Web search

With `TAVILY_API_KEY` set (free key from https://tavily.com, about 1,000 searches a
month), web search fills gaps, at most 4 searches per lookup (`INFORMANT_SEARCH_MAX`):

- **Mentions** (`web_mentions`): one search for what other sites say about the company
  (trade press, directories, partner announcements). The snippets go to the LLM with the
  other material. Social networks, people-finder sites, the company's own site, Wikipedia
  and Companies House are left out.
- **A missing website**: when domain guessing and Wikidata fail, it searches for the
  company's name and number, and accepts a site only on the usual evidence (the company
  number or postcode on its homepage, or its name in the title).
- **An unlinked annual report**: when PDF-only accounts need the report and the site
  doesn't link it, it searches for it. The usual checks still apply, including robots.txt.

Searches are cached for 7 days, so re-running a company costs nothing. `--no-search`
skips web search. Search engines' own result pages are never scraped.

## Other public records

Free, official sources, none of which needs a key. Each one can be left out with
`--skip` (comma-separated, or `all`) or `INFORMANT_SKIP`:

| `--skip`    | Section            | What it adds |
|-------------|--------------------|--------------|
| `gleif`     | `lei`              | The company's Legal Entity Identifier record from GLEIF: parent companies as the company itself reports them (often a foreign parent the UK register doesn't show), subsidiaries with LEIs, securities issued (ISINs), and why no parent is reported (for example `NATURAL_PERSONS`: owned by people, who aren't named). Found by company number, else an exact UK legal name. |
| `contracts` | `public_contracts` | UK public-sector contracts awarded to the company (Contracts Finder, since 2015): buyer, title, value and dates. An award counts only if the company is one of the named suppliers. Framework awards name many suppliers and show the whole framework's value, so only awards won alone count towards `sole_supplier_value`. |
| `donations` | `political_donations` | Donations the company made to UK parties, referendum campaigners and politicians (the Electoral Commission's register, above the reporting threshold): recipient, amount, date, cash or in kind, with totals per recipient. The current and previous names are searched, and a donation counts only if the donor's company number is the company's (or, filed without a number, the exact name). Recipients are named, including politicians (public figures; `to_individual` marks them); individual donors are never read. Other UK companies in the group (parents from the ownership chain and the LEI record, subsidiaries with LEIs) are searched on their own company numbers and listed separately in `group`. `accounts_statement` quotes what the latest accounts say about political donations (directors' report), from the iXBRL filing, the annual report or the OCR text. |
| `jobs`      | `hiring`           | Open roles from the company's public job board (Greenhouse, Lever, Workable, Ashby, SmartRecruiters, Recruitee), counted by department and location. The board is found from the site's links; without one, the brand name is tried on Greenhouse and Workable, and accepted only if the board names the same employer and has open roles. Titles, teams and places only: no people. |
| `feeds`     | `website.posts`, `website.shop` | The site's own WordPress posts (`/wp-json`) or Shopify catalogue (`/products.json`), through the crawler, so robots.txt applies. |
| `domain`    | `domain`           | DNS records (email provider, services allowed to send its email, services the domain is verified with, DMARC policy), certificate transparency logs (subdomains, other domains on the same certificates) and the Wayback Machine (first and last snapshot; a domain older than the company is noted). |

Open roles, contract awards and posts also go to the LLM with the other material.
Analytics and tag IDs found in the pages (`website.analytics_ids`) link companies in
`index.json`: `shares_analytics_with` names saved companies whose sites carry the same
ID, which usually means the same organisation runs both.

## News

Recent headlines that name the company (default: the last 90 days, up to 20 articles).

- **Sources**: the Bing News RSS feed and the GDELT news API (both free, no key), plus
  the Guardian's API if `GUARDIAN_API_KEY` is set (free key from
  https://open-platform.theguardian.com/). Google News is not used: its robots.txt
  disallows its feed. GDELT is rate-limited and sometimes unavailable; that's noted, not fatal.
- **Matching**: the search uses the brand (from the website's domain, or a distinctive
  first word of the name) and the registered name. A headline is kept only if it names
  the company; everyday-word brands like "Next" must appear capitalised.
- **Judging** (if an LLM is enabled): one call rates every headline's tone (`sentiment`:
  -1, 0 or 1) and topic (deal, results, leadership, legal, expansion or closure...), and
  whether it is really about the company. Headlines it doubts are kept with
  `relevant: false` rather than dropped, because small models misjudge some genuine
  stories (e.g. "Nubank not pursuing Monzo takeover"). Small local models also tend to
  rate almost everything neutral.
- Only headline, link, publisher, date and the feed's snippet are kept; article text
  is never downloaded.

```sh
uv run informant lookup 00502851 --news-days 30   # a shorter window
uv run informant lookup 00502851 --no-news        # skip news
```

## Cache

Crawled pages and LLM answers are kept in `.informant_cache/` (set `INFORMANT_CACHE_DIR`
to move it), so a repeat lookup takes seconds instead of minutes, and a run that failed
part-way picks up where it stopped.

- **Pages** are reused for 7 days (`INFORMANT_CACHE_DAYS`). Each page in the profile records
  `fetched_at` (when it was really downloaded) and `cached`.
- **LLM answers** are reused whenever the model, the prompt and the text sent are all
  identical. Change any of them (pick another model, or the page changes) and a fresh call
  is made, so old companies can be re-extracted with a better model without re-crawling.

```sh
uv run informant lookup 00502851 --refresh    # re-download pages (unchanged pages reuse LLM answers)
uv run informant lookup 00502851 --no-cache   # ignore the cache completely
uv run informant cache                        # where it is and how big
uv run informant cache --clear                # empty it (--pages or --llm for one half)
```

`INFORMANT_CACHE=off` turns it off. The cache holds copies of public web pages; it's
ignored by git.

## Output format

Defined in [src/informant/schema.py](src/informant/schema.py). Every section is
always present (empty when not yet populated), and every section carries
`sources` with a URL and `retrieved_at` timestamp so consumers can judge freshness.

| Section          | Source                 | Status  |
|------------------|------------------------|---------|
| identity         | Companies House        | done    |
| classification   | Companies House (SIC)  | done    |
| locations        | Companies House        | done    |
| people           | Companies House only   | done    |
| filings          | Companies House        | done    |
| financials       | filed accounts (iXBRL) | done    |
| capital          | Companies House        | done    |
| charges          | Companies House        | done    |
| ownership        | Companies House (PSC)  | done    |
| related_companies| Companies House        | done    |
| risk             | computed from the above| done    |
| website          | company website        | done    |
| products         | website + LLM          | done    |
| history          | website/Wikipedia + LLM| done    |
| funding          | website/Wikipedia + LLM| done    |
| key_facts        | website/Wikipedia + LLM| done    |
| tech_stack       | website                | done    |
| wikipedia        | Wikipedia + Wikidata   | done    |
| news             | Bing, GDELT, Guardian  | done    |
| web_mentions     | Tavily web search      | done    |
| customers        | LLM + Companies House  | done    |
| hiring           | public job boards (ATS)| done    |
| lei              | GLEIF                  | done    |
| public_contracts | Contracts Finder       | done    |
| political_donations | Electoral Commission | done   |
| domain           | DNS, cert logs, Wayback| done    |

## Using TheInformant from other programs

TheInformant is a standalone console program, and can also be driven by other
programs such as a GUI, in one of two ways.

### 1. Run it as a command (any language)

Add `--json` to any command. Then:

- **stdout** carries exactly one JSON object: `{"ok": true, ...}`, or
  `{"ok": false, "error": {"code": "...", "message": "..."}}`.
- It **never prompts** for input.
- The **exit code** says what happened:

  | Exit | `error.code` | Meaning |
  |---|---|---|
  | 0 | | success |
  | 1 | `internal_error` | unexpected bug |
  | 2 | `not_found` | no company matches |
  | 3 | `config_error` | missing or invalid settings (API key, model, options) |
  | 4 | `upstream_error` | Companies House or the network failed |

```sh
informant search "greggs" --json                 # {"results": [{company_number, title, status, ...}]}
informant lookup 00502851 --json --progress      # result on stdout, progress lines on stderr
informant lookup 00502851 --json --out-file D:/data/greggs.json
informant list --json                            # every saved profile, from index.json
informant model --json                           # model menu; `model qwen3:4b --json` sets it
informant version --json                         # program + output schema version
informant schema                                 # JSON Schema of the profile format
informant --env-file C:/path/informant.env lookup ...   # use a different settings file
```

`lookup --json` returns `path`, `index`, `matched_by` (`number` or `search`),
`alternatives` (other search matches), `warnings`, and the full `profile`.

**Progress.** With `--progress`, each step writes one JSON line to stderr:
`{"event": "progress", "stage": "crawl", "status": "done", "message": "5 pages read", "data": {...}, "ts": "..."}`.
Stages, in order: `resolve`, `companies_house`, `registry_details`, `wikipedia`,
`website_discovery`, `crawl`, `extract`, `llm`, `news`, `save`. Status is `started`, `done`,
`skipped` or `failed`. `registry_details` also sends `progress` events naming each
step ("Reading filed accounts", "Tracing ownership"...).
During staged extraction, the `llm` stage also sends `progress` events with
`data.part` and `data.parts` ("Reading part 2 of 5", then "Combining notes").

**Output folder.** Profiles go to `--out <folder>`, or `INFORMANT_OUTPUT_DIR`, or
`./output`. Files are written atomically (to a temporary file, then renamed),
so readers never see half-written files. Each folder has an `index.json` listing
every profile (number, name, status, sectors, website, generated time, file name).
If the index goes missing, it's rebuilt from the profile files.

### 2. Import it (Python)

```python
from informant.pipeline import research_company
from informant.storage import save_profile

result = research_company("00502851", progress=lambda e: print(e.to_dict()))
path, index = save_profile(result.profile, out_dir=Path("D:/data"))
```

Failures raise `informant.errors.InformantError` subclasses, which carry the
same `code` and `exit_code` as above.

## Tests

```sh
uv run pytest
```
