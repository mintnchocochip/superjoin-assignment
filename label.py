"""Label mined evidence with a local model.

The model's entire job is to say what a piece of evidence is *about*: subject,
metric, basis. It never sees a value it can alter and never writes to the
evidence table - it is handed an evidence id and returns labels keyed by that
id. A figure it mislabels stays a correctly-transcribed figure with a wrong
label, which is recoverable; a figure it rewrote would not be.

Period is deliberately not the model's call when the evidence carries a column
header. "March 31, 2024" means FY2024; that is a pure function, and a 4B model
asked to guess it returned Q4-FY2024 for a column headed March 31, 2023.

Output is schema-constrained so a small model cannot return prose or malformed
JSON, and thinking is off because this is classification, not reasoning.
"""

import json
import os
import re
import urllib.error
import urllib.request

HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:4b")

# Ollama defaults this low and silently truncates rather than erroring, which
# would quietly drop the tail of every prompt.
NUM_CTX = 4096

# Field order is generation order under constrained decoding, so is_fact comes
# last: the model commits to what the evidence is about before ruling on whether
# it is a fact at all. With is_fact first, a 4B model answered false to
# everything, including rows it then labelled correctly.
SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "metric": {"type": "string"},
        "basis": {"type": "string"},
        "is_fact": {"type": "boolean"},
    },
    "required": ["subject", "metric", "basis", "is_fact"],
}

SYSTEM = """You label evidence taken verbatim from financial and economic filings. \
Say what it is ABOUT. Never restate, recompute or correct any number.

subject  the entity the evidence concerns, e.g. "Delhivery Limited", "India"
metric   snake_case name of what is measured, e.g. revenue_from_operations,
         total_expenses, real_gdp_growth. "" if the evidence names no measure.
basis    consolidated, standalone, provisional, or revised - only if the
         evidence says so. "" otherwise. Never infer it.
is_fact  true when this states something checkable about the subject: a named
         measure with a value, or a claim about the entity. false for section
         headings, page furniture, table captions, disclaimers, and rows whose
         label names no measure.

Use "" for anything the evidence does not state."""

# A fiscal year in these filings is stated as its closing date.
FY_END = re.compile(r"(?:march|mar)\s*31,?\s*((?:19|20)\d{2})", re.IGNORECASE)
FY_SHORT = re.compile(r"\bFY\s?(\d{2}|\d{4})\b", re.IGNORECASE)
FY_SPAN = re.compile(r"\b(?:19|20)(\d{2})\s*[-/]\s*(\d{2})\b")
QUARTER = re.compile(r"\bQ([1-4])\b", re.IGNORECASE)


def period_from(text):
    """Canonical period from a column header, or None. Pure function, no model."""
    if not text:
        return None
    if m := FY_END.search(text):
        year = f"FY{m.group(1)}"
    elif m := FY_SPAN.search(text):
        year = f"FY20{m.group(2)}"
    elif m := FY_SHORT.search(text):
        raw = m.group(1)
        year = f"FY20{raw}" if len(raw) == 2 else f"FY{raw}"
    else:
        return None
    if q := QUARTER.search(text):
        return f"Q{q.group(1)}-{year}"
    return year


def _prompt(row):
    if row["kind"] == "number":
        value = " ".join(x for x in (row["currency"], row["value"], row["unit"]) if x)
        return (
            f"Row label: {row['row_label'] or '(none)'}\n"
            f"Column header: {row['col_header'] or '(none)'}\n"
            f"Value: {value}\n"
            f"Full row as printed: {row['text']}"
        )
    return f"Text excerpt:\n{row['text']}"


def label_one(row, model=None, timeout=120):
    """Return label fields for one evidence row, or None if the model failed."""
    body = json.dumps(
        {
            "model": model or MODEL,
            "system": SYSTEM,
            "prompt": _prompt(row),
            "stream": False,
            "think": False,
            "format": SCHEMA,
            "options": {"temperature": 0, "num_ctx": NUM_CTX},
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        f"{HOST}/api/generate", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.loads(json.load(resp)["response"])
    except (urllib.error.URLError, json.JSONDecodeError, KeyError, TimeoutError):
        return None

    return {
        "evidence_id": row["id"],
        "is_fact": bool(out.get("is_fact")),
        "subject": (out.get("subject") or "").strip(),
        "metric": (out.get("metric") or "").strip(),
        # The header wins whenever there is one; the model only fills the gap.
        "period": period_from(row.get("col_header")) or "",
        "basis": (out.get("basis") or "").strip(),
        "model": model or MODEL,
    }


def available():
    """Which models Ollama currently has, so the UI can say what is wrong."""
    try:
        with urllib.request.urlopen(f"{HOST}/api/tags", timeout=3) as resp:
            return [m["name"] for m in json.load(resp).get("models", [])]
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []
