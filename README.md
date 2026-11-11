# TheInformant

Researches UK companies and writes one structured JSON profile per company,
ready for a separate visualisation/categorisation program to consume.

## Setup

```sh
uv sync
cp .env.example .env   # then paste your Companies House API key into .env
uv run playwright install chromium   # optional (~150 MB): reads JavaScript-built websites
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

## How the website stage works

1. **Find the site.** Uses `--website` if given. Otherwise it tries Wikidata (only
   when `INFORMANT_CONTACT` is set), then guesses domains from the company name.
   A guessed site is kept only if it shows the company number or registered
   postcode (UK companies must show their number on their website), or has the
   name in its page title.
2. **Crawl politely.** Fetches the homepage plus up to 7 key pages (about,
   products, careers, contact, news, investors). It obeys robots.txt, waits 1s
   between requests, stays on the same site, and skips anything that blocks it.
   If a page comes back nearly empty because JavaScript builds it (e.g. monzo.com),
   it reloads that page in a headless Chromium browser, and loads the rest of that
   site the same way. Control this with `--browser auto|always|never` (default auto).
3. **Extract without an LLM.** Role email addresses (info@, sales@...),
   phone numbers, company social accounts and tech stack.
4. **Extract with an LLM** (Claude or a local Ollama model). Summary, sectors from a
   fixed list, tags, business model, target markets, products and offices.

Anything skipped or uncertain is recorded in `website.notes`.

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
| financials       | iXBRL accounts         | planned |
| website          | company website        | done    |
| products         | website + LLM          | done    |
| tech_stack       | website                | done    |
| news             | news API / RSS         | planned |
| hiring           | careers page / ATS     | planned |

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
Stages, in order: `resolve`, `companies_house`, `website_discovery`, `crawl`,
`extract`, `llm`, `save`. Status is `started`, `done`, `skipped` or `failed`.

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
