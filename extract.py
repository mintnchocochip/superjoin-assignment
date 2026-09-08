"""Mine evidence out of a PDF: numbers from table rows, and prose excerpts.

No model runs here, and none ever writes here. Evidence is produced once, at
ingest, from the document itself: values are parsed by regex so a model can
never corrupt a figure, and every excerpt is a verbatim span of the page. Each
row carries a stable content-derived id, so re-labelling attaches to existing
evidence instead of duplicating it.

Labelling - deciding what metric, period and basis a number refers to - is a
separate step that writes to a separate table. See label.py.
"""

import hashlib
import re
from collections import deque

import pymupdf

# Matches both digit-grouping conventions used across these documents:
# Indian (1,23,456) and international (1,234,567).
NUMBER = re.compile(
    r"(?P<currency>₹|Rs\.?|INR|US\$|\$)?\s*"
    r"(?P<value>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<unit>%|crores?|lakhs?|millions?|billions?|thousands?|mn\b|bn\b)?",
    re.IGNORECASE,
)

YEAR = re.compile(r"^(?:19|20)\d{2}$")
NUMERIC_CELL = re.compile(r"^[₹$(]?\s*[\d,]+(?:\.\d+)?\s*[%)]?$")

# The band that says which consolidation a group of columns reports. It sits
# above the period band in a statement, spans several columns at once, and is
# the only place that distinction is written down.
BASIS_CELL = re.compile(r"\b(consolidated|standalone)\b", re.IGNORECASE)

# A cell that names a reporting period. In these filings the column headers are
# exactly these, which is what lets a figure be tied to a period.
PERIOD_CELL = re.compile(
    r"(march\s*31|31\s*march|as\s*at|year\s*end|quarter|Q[1-4]|"
    r"FY\s*\d{2}|\b(?:19|20)\d{2}\b|\d{4}-\d{2})",
    re.IGNORECASE,
)

REFERENCE = re.compile(r"\b(note|notes|clause|section|schedule|annexure|para)\s*$", re.IGNORECASE)

# Registration and certificate numbers sit on lines full of words, so the label
# rules wave them through unless they are named explicitly.
IDENTIFIER = re.compile(
    r"\b(DIN|CIN|PAN|GSTIN|ISIN|LEI|IEC|UDIN|ACS|FCS|COP|ICSI|"
    r"membership|registration|certificate|licen[cs]e|firm|enrol?ment|unique)\b"
    r"[^.]{0,24}$",
    re.IGNORECASE,
)

LABEL = re.compile(r"[A-Za-z]{3,}(?:\s+\S+){1,}")

# The heading that states which financial statement the rows below belong to.
# This is where the consolidation basis is written down, so it has to survive
# into the evidence as quotable text.
STATEMENT = re.compile(
    r"\b(consolidated|standalone)\b[^.]{0,60}?"
    r"\b(statement|balance\s*sheet|cash\s*flow|financial\s*statements?)\b"
    r"|\bstatement\s+of\s+(profit\s+and\s+loss|changes\s+in\s+equity|cash\s*flows?)\b"
    r"|\bbalance\s*sheet\s+as\s+at\b",
    re.IGNORECASE,
)


def evidence_id(doc_id, pdf_page, row_no, start, end, text):
    """Stable id derived from where the evidence is and what it says.

    Deterministic on purpose: re-mining a document produces the same ids, so a
    re-label updates rows rather than accumulating duplicates. The row ordinal is
    in the key because a page can carry two visually identical rows, which would
    otherwise collide.
    """
    key = f"{doc_id}|{pdf_page}|{row_no}|{start}|{end}|{text}".encode("utf-8")
    return hashlib.sha256(key).hexdigest()[:16]


def _cells(words, gap=8.0):
    """Group words on one horizontal band into cells, splitting on x gaps."""
    cells, cur, x0, x1 = [], [], None, None
    for wx0, wx1, word in sorted(words):
        if cur and wx0 - x1 > gap:
            cells.append((x0, x1, " ".join(cur)))
            cur, x0 = [], None
        if x0 is None:
            x0 = wx0
        cur.append(word)
        x1 = wx1
    if cur:
        cells.append((x0, x1, " ".join(cur)))
    return cells


def _bands(page, tol=3.0):
    """Reconstruct visual rows from word positions.

    PyMuPDF's plain text extraction emits each table cell on its own line, which
    detaches a figure from its row label and its column. Grouping words by their
    y position puts the row back together, which is what makes a value
    attributable to a period.
    """
    bands = {}
    for x0, y0, x1, _y1, word, *_ in page.get_text("words"):
        bands.setdefault(round(y0 / tol), []).append((x0, x1, word))
    for key in sorted(bands):
        yield _cells(bands[key])


def _header_for(cells, headers):
    """The column header whose x-range overlaps this cell, if any."""
    x0, x1, _ = cells
    for hx0, hx1, text in headers:
        if x0 < hx1 and x1 > hx0:
            return text
    return None


def _spanning_owner(cell, spans):
    """The spanning header a column belongs to, by nearest centre.

    Period headers sit directly over their own column, so plain x-overlap finds
    them. A basis header does not: "Consolidated" is one short word centred over
    a *group* of columns, so it overlaps some of the columns it owns and none of
    the others, and overlap matching returns nothing. Assigning each column to
    the nearest header centre is what the layout actually means.

    Without this, a four-column statement - consolidated and standalone, each
    with two years - yields four figures that differ only by a qualifier nobody
    captured, and the adjudicator can say nothing about any pair of them.
    """
    if not spans:
        return None
    centre = (cell[0] + cell[1]) / 2
    return min(spans, key=lambda s: abs(centre - (s[0] + s[1]) / 2))[2]


def _reject_reason(row_text, match, label):
    digits = match.group("value").replace(",", "")
    qualified = bool(match.group("currency") or match.group("unit"))

    if not label:
        return "no row label"
    if not qualified and YEAR.match(digits):
        return "bare year"
    if not qualified and len(digits.replace(".", "")) <= 2:
        return "unqualified small number"
    if REFERENCE.search(row_text[: match.start()]):
        return "cross-reference, not a quantity"
    if IDENTIFIER.search(row_text[: match.start()]):
        return "registration or certificate number"
    return None


def _printed_page(rows):
    """The folio printed on the page, which is not its index in the PDF.

    The starter excerpts skip pages, so the two diverge and both must be stored
    or every citation points somewhere wrong.

    ponytail: last-rows heuristic. Replace with a footer-band scan if a document
    turns out to put its folio in the margin.
    """
    for row in reversed(rows[-3:]):
        text = " ".join(c[2] for c in row).strip()
        if text.isdigit() and len(text) <= 4:
            return text
    return None


def classify_row(cells):
    """What kind of row this is: 'basis', 'period', or 'data'.

    Extracted so the band rules can be checked without opening a PDF.
    """
    if not any(NUMERIC_CELL.match(c[2].strip()) for c in cells):
        if [c for c in cells if BASIS_CELL.search(c[2]) and len(c[2]) <= 60]:
            return "basis"
    labelled = [c for c in cells if PERIOD_CELL.search(c[2])]
    if labelled and len(labelled) >= max(1, len(cells) - 1):
        return "period"
    return "data"


def mine(pdf_path, doc_id):
    """Yield evidence rows: mined numbers, and prose excerpts worth labelling."""
    doc = pymupdf.open(pdf_path)
    try:
        for pdf_page, page in enumerate(doc):
            rows = list(_bands(page))
            printed = _printed_page(rows)
            row_label, headers, bases = None, [], []
            # The statement heading a row sits under. It is what states the
            # consolidation basis, so it has to reach the labeller as quotable text.
            section, context = None, deque(maxlen=3)

            for row_no, cells in enumerate(rows):
                # Join once, recording where each cell landed, so a repeated value
                # in a different column still gets its own offsets and its own id.
                parts, spans, pos = [], [], 0
                for cell in cells:
                    parts.append(cell[2])
                    spans.append((pos, pos + len(cell[2])))
                    pos += len(cell[2]) + 1
                text = " ".join(parts)
                if not text.strip():
                    continue

                # Only a line that names a statement becomes the section. A general
                # "short line without numbers" heuristic just latches onto the last
                # sentence of body prose, and this line's job is to carry the
                # consolidation basis as text the labeller can quote.
                if STATEMENT.search(text):
                    section = text.strip()
                    # A new statement invalidates the column layout above it.
                    headers, bases = [], []

                # A band naming consolidations owns the columns beneath it until
                # the next statement; a band of period names is the column header
                # for the rows below. classify_row decides which, so the rule the
                # self-check exercises is the rule that actually runs here.
                kind = classify_row(cells)
                if kind == "basis":
                    bases = [c for c in cells if BASIS_CELL.search(c[2])]
                    continue
                if kind == "period":
                    headers = [c for c in cells if PERIOD_CELL.search(c[2])]
                    continue

                # The leading non-numeric cells name the row.
                lead = [c[2] for c in cells if not NUMERIC_CELL.match(c[2].strip())]
                if lead and LABEL.search(" ".join(lead)):
                    row_label = " ".join(lead).strip()

                numeric = [
                    (cell, span)
                    for cell, span in zip(cells, spans)
                    if NUMERIC_CELL.match(cell[2].strip())
                ]
                for cell, (start, end) in numeric:
                    for match in NUMBER.finditer(cell[2]):
                        reason = _reject_reason(text, match, row_label)
                        yield {
                            "kind": "number",
                            "row_no": row_no,
                            "pdf_page": pdf_page,
                            "printed_page": printed,
                            "text": text,
                            "row_label": row_label,
                            "section": section,
                            "context": "\n".join(context),
                            "col_header": _header_for(cell, headers),
                            "col_basis": _spanning_owner(cell, bases),
                            "value": match.group("value"),
                            "currency": (match.group("currency") or "").strip(),
                            "unit": (match.group("unit") or "").strip(),
                            "char_start": start + match.start("value"),
                            "char_end": start + match.end("value"),
                            "accepted": reason is None,
                            "reject_reason": reason,
                        }

                # Prose worth showing a model. Whether it actually states a fact
                # is the model's call, not a regex's.
                if not numeric and 60 <= len(text) <= 400 and LABEL.search(text):
                    yield {
                        "kind": "text",
                        "row_no": row_no,
                        "pdf_page": pdf_page,
                        "printed_page": printed,
                        "text": text,
                        "row_label": None,
                        "col_basis": None,
                        "section": section,
                        "context": "\n".join(context),
                        "col_header": None,
                        "value": None,
                        "currency": "",
                        "unit": "",
                        "char_start": 0,
                        "char_end": len(text),
                        "accepted": True,
                        "reject_reason": None,
                    }

                context.append(text.strip())
    finally:
        doc.close()


def cover_text(pdf_path, pages=2, limit=2500):
    """The opening pages as text, for identifying what the document is.

    Same band reconstruction as the miner, so the entity name on a title page
    survives as one line instead of arriving one word per line.
    """
    doc = pymupdf.open(pdf_path)
    try:
        out = []
        for page in list(doc)[:pages]:
            for cells in _bands(page):
                line = " ".join(c[2] for c in cells).strip()
                if line:
                    out.append(line)
        return "\n".join(out)[:limit]
    finally:
        doc.close()




def mine_document(pdf_path, doc_id):
    """Mine a document and stamp every row with its stable id. Returns (pages, rows)."""
    doc = pymupdf.open(pdf_path)
    pages = doc.page_count
    doc.close()

    rows = []
    for row in mine(pdf_path, doc_id):
        row["id"] = evidence_id(
            doc_id, row["pdf_page"], row["row_no"], row["char_start"], row["char_end"],
            row["text"],
        )
        row["doc_id"] = doc_id
        rows.append(row)
    return pages, rows


if __name__ == "__main__":
    # A four-column statement, laid out as these filings lay them out: one basis
    # band spanning two year columns each, centred over its own group.
    #
    #                  Consolidated              Standalone
    #            Mar 31 2024  Mar 31 2023   Mar 31 2024  Mar 31 2023
    #  Revenue      81,415.38    72,253.01     74,540.82    66,586.61
    basis_band = [(200.0, 265.0, "Consolidated"), (340.0, 395.0, "Standalone")]
    period_band = [
        (180.0, 230.0, "March 31, 2024"), (250.0, 300.0, "March 31, 2023"),
        (320.0, 370.0, "March 31, 2024"), (390.0, 440.0, "March 31, 2023"),
    ]
    data = [
        (60.0, 140.0, "Revenue from operations"),
        (185.0, 228.0, "81,415.38"), (255.0, 298.0, "72,253.01"),
        (325.0, 368.0, "74,540.82"), (395.0, 438.0, "66,586.61"),
    ]

    # Every figure must land under the right consolidation. Before this, all four
    # carried no basis at all and were mutually indistinguishable.
    owners = [_spanning_owner(c, basis_band) for c in data[1:]]
    assert owners == ["Consolidated", "Consolidated", "Standalone", "Standalone"], owners

    # Periods keep using overlap, because a period header sits over its own column.
    periods = [_header_for(c, period_band) for c in data[1:]]
    assert periods == ["March 31, 2024", "March 31, 2023",
                       "March 31, 2024", "March 31, 2023"], periods

    # Together the four are now distinct, which is the whole point.
    assert len(set(zip(owners, periods))) == 4

    # Row classification: the basis band must not be mistaken for data, and a
    # data row that merely mentions "consolidated" must not become a band.
    assert classify_row(basis_band) == "basis"
    assert classify_row(period_band) == "period"
    assert classify_row(data) == "data"
    assert classify_row([(60.0, 300.0, "Tax expense recognised in consolidated financials"),
                         (320.0, 360.0, "885.20")]) == "data"

    # One spanning header owns everything beneath it.
    assert _spanning_owner(data[1], [(200.0, 400.0, "Consolidated")]) == "Consolidated"
    assert _spanning_owner(data[1], []) is None

    print("extract self-check ok")
