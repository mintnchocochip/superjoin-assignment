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


def mine(pdf_path, doc_id):
    """Yield evidence rows: mined numbers, and prose excerpts worth labelling."""
    doc = pymupdf.open(pdf_path)
    try:
        for pdf_page, page in enumerate(doc):
            rows = list(_bands(page))
            printed = _printed_page(rows)
            row_label, headers = None, []
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

                # A row of period names is the column header for the rows below it.
                labelled = [c for c in cells if PERIOD_CELL.search(c[2])]
                if labelled and len(labelled) >= max(1, len(cells) - 1):
                    headers = labelled
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
