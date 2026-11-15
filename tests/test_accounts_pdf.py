"""Reading accounts from PDFs: finding the annual report, choosing pages, and the
LLM step with its checks. No real downloads, OCR or model calls."""

from datetime import date

import httpx
import pytest
import respx

from informant.llm.accounts import extract_accounts, figure_in_text, parse_date, parse_figure
from informant.llm.accounts import FactsExtraction, FiguresExtraction
from informant.llm.backends.base import Completion
from informant.sources.accounts_pdf import (
    PdfPage,
    date_forms,
    find_annual_report,
    report_candidates,
    select_pages,
    verify_report,
)
from informant.sources.website.fetch import Crawler

INCOME = """Consolidated income statement
for the 52 weeks ended 27 December 2025
                                   2025      2024
                                   £m        £m
Revenue                         2,148.7   2,014.4
Cost of sales                  (782.4)   (737.9)
Gross profit                    1,366.3   1,276.5
Operating profit                  187.5     195.3
Profit before tax                 178.1     189.8
Profit for the financial year     133.6     140.2
"""
BALANCE = """Consolidated balance sheet
At 27 December 2025
Total assets 1,812.0 1,701.3
Cash and cash equivalents 46.3 125.3
Net assets 621.4 584.9
Total equity 621.4 584.9
""" + " ".join(str(1000 + i) for i in range(20))
NOTE = """12. Property, plant and equipment
Any impairment charge is charged to distribution costs in the income statement.
""" + " ".join(f"{i},{i:03d}.{i % 10}" for i in range(100, 160))
CONTENTS = """Contents
Strategic report 2
Consolidated income statement 130
Consolidated balance sheet 131
Consolidated statement of cash flows 133
Notes to the accounts 140
Independent auditor's report 120"""
FACTS_PAGE = """Directors' report
Principal activities
The principal activity of the Group is the retailing of sandwiches, savouries and other bakery products.
Substantial shareholdings
Baillie Gifford & Co 10.2%
BlackRock Inc 5.1%
Average number of employees 32,481 (2024: 31,800)
Independent auditor's report to the members of Greggs plc
Opinion: we have audited ... signed for and on behalf of KPMG LLP, Statutory Auditor."""


# --- parsing helpers --------------------------------------------------------------------


def test_parse_figure_and_grounding():
    assert parse_figure("2,148.7") == 2148.7
    assert parse_figure("(782.4)") == -782.4 and parse_figure("-12") == -12
    assert parse_figure("£1,000") == 1000 and parse_figure("") is None and parse_figure("-") is None
    compact = "Revenue2,148.72,014.4".replace(",", "")
    assert figure_in_text("2,148.7", compact) and not figure_in_text("2,149.7", compact)


def test_parse_date():
    assert parse_date("27 December 2025", None) == date(2025, 12, 27)
    assert parse_date("31 Mar 2026", None) == date(2026, 3, 31)
    assert parse_date("2025-06-30", None) == date(2025, 6, 30)
    assert parse_date("the year", date(2024, 1, 1)) == date(2024, 1, 1)


def test_date_forms_cover_common_writings():
    forms = date_forms(date(2026, 3, 31))
    assert "31 March 2026" in forms and "31/03/2026" in forms and "2026-03-31" in forms


# --- page selection -----------------------------------------------------------------------


def test_select_pages_picks_statements_not_notes_or_contents():
    texts = [CONTENTS, "Chairman's statement", NOTE, INCOME + " ".join(str(i * 7) for i in range(100, 130)),
             BALANCE, FACTS_PAGE]
    chosen = {topic: p.number for p in select_pages(texts) for topic in p.topics}
    assert chosen["income_statement"] == 4  # not the contents page (1) or the note (3)
    assert chosen["balance_sheet"] == 5
    assert chosen["activities"] == 6 and chosen["shareholders"] == 6 and chosen["employees"] == 6


def test_heading_inside_a_sentence_doesnt_count():
    sentence = ("Gains are recognised in the income statement.\n"
                + " ".join(f"{i},{i:03d}" for i in range(100, 160)))
    assert select_pages([sentence]) == []


# --- finding the annual report ---------------------------------------------------------------


def test_report_candidates_prefer_this_years_report():
    links = [
        ("https://x.example/docs/pillar-3-2025.pdf", "Pillar 3 Statement"),
        ("https://x.example/docs/annual-report-2024.pdf", "Annual Report 2024"),
        ("https://x.example/docs/annual-report-2025.pdf", "Annual Report 2025"),
        ("https://cdn.example/49885-acme-ar25-interactive.pdf", "Download PDF"),
        ("https://x.example/docs/modern-slavery-2025.pdf", "Modern Slavery Statement"),
        ("https://x.example/investors", "Investors"),
    ]
    ranked = [u for _, u in report_candidates(links, 2025)]
    assert ranked[:2] == ["https://x.example/docs/annual-report-2025.pdf",
                          "https://cdn.example/49885-acme-ar25-interactive.pdf"]
    assert not any("pillar" in u or "slavery" in u for u in ranked)
    assert ranked[-1] == "https://x.example/docs/annual-report-2024.pdf"  # wrong year ranks last


def test_verify_report_needs_name_and_exact_year_end():
    texts = ["Acme Widgets plc Annual Report", "for the year ended 31 March 2026"]
    assert verify_report(texts, "ACME WIDGETS PLC", date(2026, 3, 31))
    assert not verify_report(texts, "ACME WIDGETS PLC", date(2025, 3, 31))
    assert not verify_report(texts, "OTHER COMPANY LTD", date(2026, 3, 31))


def minimal_pdf(text: str) -> bytes:
    """A tiny but valid PDF with one page of text."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


REPORT_TEXT = ("Acme Widgets plc Annual Report and Accounts for the year ended 31 March 2026 "
               + "Revenue and profit figures follow for the group. " * 12)


@respx.mock
def test_find_annual_report_follows_corporate_site_and_verifies():
    respx.get("https://acme.example/robots.txt").respond(404)
    respx.get("https://corporate.acme.co.uk/robots.txt").respond(404)
    respx.get("https://cdn.acme.co.uk/robots.txt").respond(404)
    respx.get("https://corporate.acme.co.uk/").respond(html=(
        "<html><body><a href='/investors/reports'>Reports and results</a></body></html>"))
    respx.get("https://corporate.acme.co.uk/investors/reports").respond(html=(
        "<html><body><a href='https://cdn.acme.co.uk/acme-ar26.pdf'>Download PDF</a>"
        "<a href='https://cdn.acme.co.uk/acme-pay-gap-2026.pdf'>Pay gap</a></body></html>"))
    respx.get("https://cdn.acme.co.uk/acme-ar26.pdf").respond(content=minimal_pdf(REPORT_TEXT))
    seeds = [("https://corporate.acme.co.uk/", "Corporate")]
    with Crawler(delay=0) as crawler, httpx.Client() as http:
        found = find_annual_report(crawler, company_name="ACME WIDGETS PLC", made_up=date(2026, 3, 31),
                                   website="https://acme.example/", seed_links=seeds, http=http)
    assert found is not None and found[0] == "https://cdn.acme.co.uk/acme-ar26.pdf"
    assert "31 March 2026" in found[1][0]


@respx.mock
def test_find_annual_report_respects_robots():
    respx.get("https://cdn.acme.co.uk/robots.txt").respond(text="User-agent: *\nDisallow: /\n")
    pdf = respx.get("https://cdn.acme.co.uk/acme-annual-report-2026.pdf").respond(
        content=minimal_pdf(REPORT_TEXT))
    seeds = [("https://cdn.acme.co.uk/acme-annual-report-2026.pdf", "Annual report 2026")]
    with Crawler(delay=0) as crawler, httpx.Client() as http:
        found = find_annual_report(crawler, company_name="ACME WIDGETS PLC", made_up=date(2026, 3, 31),
                                   website=None, seed_links=seeds, http=http)
    assert found is None and pdf.call_count == 0


# --- the LLM step -----------------------------------------------------------------------------


def fake_complete(figures: dict, facts: dict):
    def complete(system, user, schema):
        if schema is FiguresExtraction:
            return Completion(FiguresExtraction(**figures), "ollama/test")
        return Completion(FactsExtraction(**facts), "ollama/test")
    return complete


def year(**kw):
    base = dict(period_end="", currency="GBP", unit="millions", turnover="", gross_profit="",
                operating_profit="", profit_before_tax="", profit_after_tax="", total_assets="",
                net_assets="", cash="")
    base.update(kw)
    return base


def test_extract_accounts_converts_units_and_drops_unsupported_figures():
    pages = [PdfPage(1, INCOME, ["income_statement"]), PdfPage(2, BALANCE, ["balance_sheet"]),
             PdfPage(3, FACTS_PAGE, ["activities", "shareholders", "employees", "auditor"])]
    figures = {"years": [
        year(period_end="27 December 2025", turnover="2,148.7", gross_profit="1,366.3",
             operating_profit="187.5", profit_before_tax="178.1", profit_after_tax="133.6",
             total_assets="1,812.0", net_assets="621.4", cash="46.3"),
        year(period_end="28 December 2024", turnover="2,014.4", operating_profit="999.9"),  # invented
    ]}
    facts = {"principal_activities": "The retailing of sandwiches, savouries and other bakery products.",
             "auditor": "KPMG LLP", "ultimate_controlling_party": "Imaginary Holdings Group",
             "employees": "32,481",
             "substantial_shareholders": [{"name": "Baillie Gifford & Co", "stake": "10.2%"},
                                          {"name": "Jane Smith", "stake": "4%"}]}
    r = extract_accounts("GREGGS PLC", pages, date(2025, 12, 27), fake_complete(figures, facts))

    latest, prior = r.periods
    assert latest["period_end"] == "2025-12-27" and latest["turnover"] == 2_148_700_000
    assert latest["profit_loss"] == 133_600_000 and latest["cash"] == 46_300_000
    assert latest["net_assets"] == 621_400_000 and latest["employees"] == 32481
    assert prior["period_end"] == "2024-12-28" and prior["turnover"] == 2_014_400_000
    assert "operating_profit" not in prior  # 999.9 isn't on the page
    assert any("dropped 1 figure" in n for n in r.notes)
    assert r.auditor == "KPMG LLP" and r.principal_activities.startswith("The retailing")
    assert r.ultimate_controlling_party is None  # not in the text
    assert r.shareholders == [("Baillie Gifford & Co", "10.2%")]  # the person isn't in the text either


def test_extract_accounts_handles_a_failed_model_call():
    def broken(system, user, schema):
        return Completion(None, "ollama/test", ["could not reach Ollama"])
    r = extract_accounts("X", [PdfPage(1, INCOME, ["income_statement"])], None, broken)
    assert r.periods == [] and "figures not read" in r.notes[0]


def test_activities_need_an_activities_page_and_stakes_need_a_figure():
    board_page = PdfPage(1, "Board activities: strategy, culture, supply chain. "
                         "Silchester International Investors LLP 5.00% J.P. Morgan Securities Ple",
                         ["shareholders"])
    facts = {"principal_activities": "Strategy, culture and supply chain.", "auditor": "",
             "ultimate_controlling_party": "", "employees": "",
             "substantial_shareholders": [
                 {"name": "Silchester International Investors LLP", "stake": "notable"},
                 {"name": "J.P. Morgan Securities Ple", "stake": "5.15%"}]}
    r = extract_accounts("GREGGS PLC", [board_page], None, fake_complete({"years": []}, facts))
    assert r.principal_activities is None  # no "principal activities" page was read
    assert r.shareholders == [("Silchester International Investors LLP", None),
                              ("J.P. Morgan Securities plc", "5.15%")]
