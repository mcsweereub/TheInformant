# TheInformant

Researches UK companies and writes one structured JSON profile per company,
ready for a separate visualisation/categorisation program to consume.

## Setup

```sh
uv sync
cp .env.example .env   # then paste your Companies House API key into .env
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

## Tests

```sh
uv run pytest
```
