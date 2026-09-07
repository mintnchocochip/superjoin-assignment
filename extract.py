"""Mine candidate numeric facts out of a PDF.

No model is involved here, deliberately. Numbers are found with a regex so their
values can never be corrupted by a model, and the evidence quote is the source
line the number was mined from, so it is verbatim by construction rather than by
a grounding check.

What this does NOT do yet: label a candidate with its metric, period or basis.
That is the one job a local model gets, and it lands in a later phase.
"""

import re

import pymupdf

# Matches both digit-grouping conventions used across these documents:
# Indian (1,23,456) and international (1,234,567), with or without a currency
# marker and with or without a magnitude word.
NUMBER = re.compile(
    r"(?P<currency>₹|Rs\.?|INR|US\$|\$)?\s*"
    r"(?P<value>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<unit>%|crores?|lakhs?|millions?|billions?|thousands?|mn\b|bn\b)?",
    re.IGNORECASE,
)

YEAR = re.compile(r"^(?:19|20)\d{2}$")

# Words that mean the following number is a reference, not a quantity.
REFERENCE = re.compile(r"\b(note|notes|clause|section|schedule|annexure|para)\s*$", re.IGNORECASE)

# Registration and certificate numbers. These sit on lines with plenty of words, so
# the label rules wave them through; they have to be named explicitly.
IDENTIFIER = re.compile(
    r"\b(DIN|CIN|PAN|GSTIN|ISIN|LEI|IEC|UDIN|ACS|FCS|COP|ICSI|"
    r"membership|registration|certificate|licen[cs]e|firm|enrol?ment|unique)\b"
    r"[^.]{0,24}$",
    re.IGNORECASE,
)

# A line that reads as a row label: at least two words of actual prose.
LABEL = re.compile(r"[A-Za-z]{3,}(?:\s+\S+){1,}")


def _reject_reason(line, match, label):
    """Why this candidate is not a fact, or None if it looks like one.

    Rejected candidates are kept rather than dropped so the reasons can be
    inspected in the UI and these rules tuned against real pages.
    """
    stripped = line.strip()
    digits = match.group("value").replace(",", "")
    qualified = bool(match.group("currency") or match.group("unit"))

    if not label:
        return "no label text on the line or above it"
    if stripped == match.group(0).strip() and not label:
        return "isolated number, likely a folio or index"
    if not qualified and YEAR.match(digits):
        return "bare year"
    if not qualified and len(digits.replace(".", "")) <= 2:
        return "unqualified small number"
    if REFERENCE.search(line[: match.start()]):
        return "cross-reference, not a quantity"
    if IDENTIFIER.search(line[: match.start()]):
        return "registration or certificate number"
    return None


def _printed_page(lines):
    """The page number printed on the page, which is not its index in the PDF.

    The starter excerpts skip pages, so the printed folio and the PDF index
    diverge. Both are stored or every citation points at the wrong page.

    ponytail: last-lines heuristic. Replace with a header/footer band scan if a
    document turns out to put its folio in the margin.
    """
    for line in reversed(lines[-3:]):
        candidate = line.strip()
        if candidate.isdigit() and len(candidate) <= 4:
            return candidate
    return None


def mine(pdf_path):
    """Yield one dict per candidate number found in the document."""
    doc = pymupdf.open(pdf_path)
    try:
        for page_index, page in enumerate(doc):
            lines = page.get_text().splitlines()
            printed = _printed_page(lines)

            # PyMuPDF emits each table cell on its own line, so a figure and its row
            # label are usually separate lines. Carry the nearest preceding label
            # forward, or the entire financial statements section extracts as
            # unlabelled numbers.
            label = None

            for line in lines:
                stripped = line.strip()
                if LABEL.search(stripped) and not NUMBER.fullmatch(stripped):
                    label = stripped

                indent = len(line) - len(line.lstrip())
                for match in NUMBER.finditer(line):
                    on_line = LABEL.search(stripped) is not None
                    context = stripped if on_line else label
                    reason = _reject_reason(line, match, context)
                    yield {
                        "pdf_page": page_index,
                        "printed_page": printed,
                        "value": match.group("value"),
                        "currency": (match.group("currency") or "").strip(),
                        "unit": (match.group("unit") or "").strip(),
                        "line": stripped,
                        "label": None if on_line else label,
                        # Offsets are into the stripped line, which is what is stored.
                        "char_start": match.start("value") - indent,
                        "char_end": match.end("value") - indent,
                        "accepted": reason is None,
                        "reject_reason": reason,
                    }
    finally:
        doc.close()


def page_count(pdf_path):
    doc = pymupdf.open(pdf_path)
    try:
        return doc.page_count
    finally:
        doc.close()
