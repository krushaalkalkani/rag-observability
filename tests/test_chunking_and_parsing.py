"""Fast offline tests. A whitespace "tokenizer" stands in for the real one,
so 1 token = 1 word and the numbers are easy to reason about."""

import re

from rag.chunking import chunk_document
from rag.parse import parse_filing


class WordTokenizer:
    def __call__(self, text, **kwargs):
        return {"offset_mapping": [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]}


def make_doc(blocks):
    return {"ticker": "TST", "company": "Test Co", "fiscal_year": 2025, "source_url": "http://x", "blocks": blocks}


def test_chunks_overlap_and_ids_are_stable():
    words = " ".join(f"w{i}" for i in range(250))
    doc = make_doc([{"page": 1, "section": "Item 1. Business", "text": words}])
    chunks = chunk_document(doc, WordTokenizer(), chunk_size=100, overlap=20)

    assert [c["chunk_id"] for c in chunks] == ["TST-2025-00000", "TST-2025-00001", "TST-2025-00002"]
    assert chunks[0]["text"].split()[-20:] == chunks[1]["text"].split()[:20]  # 20-word overlap
    assert chunks[-1]["text"].split()[-1] == "w249"  # nothing lost at the end
    assert chunk_document(doc, WordTokenizer(), 100, 20) == chunks  # deterministic => idempotent


def test_chunks_never_cross_sections_and_track_pages():
    doc = make_doc(
        [
            {"page": 1, "section": "Item 1. Business", "text": "a " * 30},
            {"page": 2, "section": "Item 1. Business", "text": "b " * 30},
            {"page": 2, "section": "Item 1A. Risk Factors", "text": "c " * 30},
        ]
    )
    chunks = chunk_document(doc, WordTokenizer(), chunk_size=100, overlap=10)
    assert [c["section"] for c in chunks] == ["Item 1. Business", "Item 1A. Risk Factors"]
    assert (chunks[0]["page_start"], chunks[0]["page_end"]) == (1, 2)
    assert "c" not in chunks[0]["text"]


def test_parse_finds_pages_and_skips_table_of_contents():
    filler = "<p>" + "Lorem ipsum dolor sit amet. " * 20 + "</p>"
    html = f"""<html><body>
      <table><tr><td>Item 1.</td><td>Business</td><td>3</td></tr>
             <tr><td>Item 1A.</td><td>Risk Factors</td><td>9</td></tr></table>
      <div style="page-break-after: always"></div>
      <p>Item 1. Business</p>{filler}
      <div style="page-break-after:always"></div>
      <p>Item 1A. Risk Factors</p><p>We face <span>intense</span> competition.</p>
    </body></html>"""
    blocks = parse_filing(html)

    toc = blocks[0]
    assert toc["page"] == 1 and toc["section"] == "Front Matter"  # TOC lines are NOT section starts
    risk = [b for b in blocks if b["section"] == "Item 1A. Risk Factors"]
    assert risk[0]["page"] == 3
    assert "We face intense competition." in risk[0]["text"]  # inline <span> stays in the sentence


def test_running_page_headers_are_not_section_starts():
    filler = "<p>" + "Text. " * 50 + "</p>"
    html = f"""<html><body>
      <p>Item 1</p><p>ITEM 1. BUSINESS</p>{filler}
      <div style="page-break-after:always"></div>
      <p>Item 1</p>{filler}
      <div style="page-break-after:always"></div>
      <p>Item 1A</p><p>ITEM 1A. RISK FACTORS</p>{filler}
      <div style="page-break-after:always"></div>
      <p>Item 1A</p>{filler}
    </body></html>"""
    sections_by_page = {}
    for b in parse_filing(html):
        sections_by_page.setdefault(b["page"], []).append(b["section"])
    assert sections_by_page[2] == ["Item 1. Business"]  # running header "Item 1" changes nothing
    assert sections_by_page[3][-1] == "Item 1A. Risk Factors"  # the real heading starts 1A
    assert sections_by_page[4] == ["Item 1A. Risk Factors"]


def test_toc_rows_with_page_ranges_are_ignored():
    filler = "<p>" + "Text. " * 50 + "</p>"
    html = f"""<html><body>
      <table><tr><td>Item 1A.</td><td>Risk Factors.</td><td>9-31</td></tr></table>
      <p>Item 1. Business.</p>{filler}<p>Item 1A. Risk Factors.</p>{filler}
    </body></html>"""
    blocks = parse_filing(html)
    assert blocks[0]["section"] == "Front Matter"
    assert blocks[-1]["section"] == "Item 1A. Risk Factors"
