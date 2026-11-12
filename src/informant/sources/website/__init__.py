"""The company website as a source.

    discovery.py  find the official site
    fetch.py      polite crawler (robots.txt, delays, browser fallback)
    renderer.py   headless browser for JavaScript-built pages
    pages.py      which pages to read
    parse.py      HTML -> text, title, links
    extract.py    contacts, social links, tech stack, ownership check (no LLM)
    research.py   the website stage: discover -> crawl -> extract -> merge into a profile

Deliberately imports nothing (research.py depends on the llm package, which
depends on parse.py; importing it here would create an import cycle).
"""
