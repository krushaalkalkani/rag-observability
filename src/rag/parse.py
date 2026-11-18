"""Turn a 10-K HTML file into clean text blocks with page + section metadata.

Output shape (one block = consecutive lines on the same page and in the same section):
    [{"page": 12, "section": "Item 1A. Risk Factors", "text": "..."}, ...]
"""

import re
import warnings

from bs4 import BeautifulSoup, NavigableString, XMLParsedAsHTMLWarning

# 10-Ks are "inline XBRL" (XHTML). Parsing them as HTML is what we want: it is
# forgiving of the messy markup and gives us the same text a browser shows.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

PAGE_MARK = "␞PAGEBREAK␞"  # an unusual string that never appears in real text
BLOCK_TAGS = ["p", "div", "br", "li", "table", "h1", "h2", "h3", "h4", "h5", "h6", "hr"]
PAGE_BREAK_STYLE = re.compile(r"(page-break|break)-(before|after)\s*:\s*(always|page)", re.I)

# The standard 10-K items. Using fixed titles keeps section metadata consistent
# across companies even when each company formats its headings differently.
ITEM_TITLES = {
    "1": "Business",
    "1A": "Risk Factors",
    "1B": "Unresolved Staff Comments",
    "1C": "Cybersecurity",
    "2": "Properties",
    "3": "Legal Proceedings",
    "4": "Mine Safety Disclosures",
    "5": "Market for Registrant's Common Equity",
    "6": "[Reserved]",
    "7": "Management's Discussion and Analysis",
    "7A": "Quantitative and Qualitative Disclosures About Market Risk",
    "8": "Financial Statements and Supplementary Data",
    "9": "Changes in and Disagreements with Accountants",
    "9A": "Controls and Procedures",
    "9B": "Other Information",
    "9C": "Disclosure Regarding Foreign Jurisdictions that Prevent Inspections",
    "10": "Directors, Executive Officers and Corporate Governance",
    "11": "Executive Compensation",
    "12": "Security Ownership of Certain Beneficial Owners and Management",
    "13": "Certain Relationships and Related Transactions",
    "14": "Principal Accountant Fees and Services",
    "15": "Exhibits and Financial Statement Schedules",
    "16": "Form 10-K Summary",
}
ITEM_RE = re.compile(r"^item\s*(\d{1,2}[abc]?)\b\s*[.:\-–—]?", re.I)
FRONT_MATTER = "Front Matter"


def html_to_pages(html: str) -> list[list[str]]:
    """Return the document as pages, each page a list of non-empty text lines."""
    soup = BeautifulSoup(html, "lxml")

    # 1. Remove things a human reader never sees.
    for tag in soup(["script", "style", "head", "title"]):
        tag.decompose()
    for tag in soup.find_all(re.compile(r"^ix:header$")):  # hidden XBRL data block
        tag.decompose()
    for tag in soup.find_all(style=re.compile(r"display\s*:\s*none", re.I)):
        tag.decompose()

    # 2. Mark page breaks. Filings are printed documents converted to HTML;
    #    the printer page breaks survive as CSS "page-break-before/after".
    for tag in soup.find_all(style=PAGE_BREAK_STYLE):
        marker = NavigableString(f"\n{PAGE_MARK}\n")
        if "before" in tag["style"].lower():
            tag.insert_before(marker)
        else:
            tag.insert_after(marker)

    # 3. Flatten each table row to one line: "Revenue | 96,773 | 81,462".
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        tr.replace_with(NavigableString(" | ".join(cells) + "\n" if cells else ""))

    # 4. Put a newline after block elements so paragraphs stay on separate lines,
    #    while inline <span>s inside a paragraph stay joined.
    for tag in soup.find_all(BLOCK_TAGS):
        if tag.parent is not None:
            tag.insert_after(NavigableString("\n"))

    text = soup.get_text().replace("\xa0", " ").replace("​", "")

    pages = []
    for raw_page in text.split(PAGE_MARK):
        lines = [re.sub(r"\s+", " ", line).strip() for line in raw_page.split("\n")]
        pages.append([line for line in lines if line])
    return pages


# Table-of-contents row ends with a page number or range: "| 14", "| F-1", "| 9-31"
TOC_ROW_RE = re.compile(r"\|\s*[A-Z]?-?\d+(\s*[-–]\s*\d+)?\s*$")


def find_section_headings(lines: list[str]) -> dict[int, str]:
    """Map line index -> section name for lines that start a 10-K Item.

    "Item 1A" shows up in three kinds of places:
      1. the table of contents:   "Item 1A. | Risk Factors | 14"   (ends in a page number)
      2. running page headers:    "Item 1A"                        (no title, on every page)
      3. the real section start:  "ITEM 1A. RISK FACTORS"           <- what we want
    So for each item we take the FIRST line that has a title after the number and
    is not a TOC row. If an item has no such line, fall back to the last bare match.
    """
    strong: dict[str, int] = {}
    weak: dict[str, int] = {}
    for i, line in enumerate(lines):
        if len(line) > 150:  # real headings are short; long lines are sentences
            continue
        m = ITEM_RE.match(line)
        if not (m and m.group(1).upper() in ITEM_TITLES):
            continue
        item = m.group(1).upper()
        has_title = re.search(r"[A-Za-z]{3,}", line[m.end():]) is not None
        if has_title and not TOC_ROW_RE.search(line):
            strong.setdefault(item, i)
        weak[item] = i
    chosen = {item: strong.get(item, weak[item]) for item in weak}
    return {i: f"Item {item}. {ITEM_TITLES[item]}" for item, i in chosen.items()}


def parse_filing(html: str) -> list[dict]:
    pages = html_to_pages(html)
    flat = [(page_no, line) for page_no, lines in enumerate(pages, start=1) for line in lines]
    headings = find_section_headings([line for _, line in flat])

    blocks: list[dict] = []
    section = FRONT_MATTER
    for i, (page_no, line) in enumerate(flat):
        section = headings.get(i, section)
        if blocks and blocks[-1]["page"] == page_no and blocks[-1]["section"] == section:
            blocks[-1]["lines"].append(line)
        else:
            blocks.append({"page": page_no, "section": section, "lines": [line]})
    return [{"page": b["page"], "section": b["section"], "text": "\n".join(b["lines"])} for b in blocks]
