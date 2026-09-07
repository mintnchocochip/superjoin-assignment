"""Label mined evidence with a local model, and refuse anything it cannot quote.

The model never reads, alters or discovers a number. It is handed one already
extracted figure and asked what that figure measures, under which qualifiers.

Every qualifier it returns must arrive with a verbatim phrase from the text it
was shown. A validator then checks that phrase really is a substring of that
text and drops the field if it is not. The prompt can only ask for grounding;
this is what enforces it, and it is what stops a 4B model from deciding a figure
is "provisional" because financial figures often are.

Two other things are structural rather than instructed:

- Period comes from the column header when there is one. "March 31, 2024" means
  FY2024; that is a pure function, and asked to guess it this model returned
  Q4-FY2024 for a column headed March 31, 2023.
- Key order is generation order under constrained decoding, so is_fact and
  confidence sit last: the model commits to what the evidence is about before
  ruling on whether it is a fact. With is_fact first it answered false to
  everything, including rows it went on to label correctly.
"""

import json
import os
import re
import urllib.error
import urllib.request

HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:4b")
NUM_CTX = 4096

GROUNDED = ("metric", "period", "basis", "vintage", "scope")
UNITS = {"INR_crore", "INR_lakh", "INR_million", "INR_billion", "percent", "ratio", "count"}
BASES = {"consolidated", "standalone"}
VINTAGES = {"audited", "provisional", "restated", "unaudited"}

# Property order is load-bearing: it is the order the model generates in.
SCHEMA = {
    "type": "object",
    "properties": {
        "metric": {"type": ["string", "null"]},
        "metric_evidence": {"type": ["string", "null"]},
        "unit": {"type": ["string", "null"], "enum": sorted(UNITS) + [None]},
        "period": {"type": ["string", "null"]},
        "period_evidence": {"type": ["string", "null"]},
        "basis": {"type": ["string", "null"], "enum": sorted(BASES) + [None]},
        "basis_evidence": {"type": ["string", "null"]},
        "vintage": {"type": ["string", "null"], "enum": sorted(VINTAGES) + [None]},
        "vintage_evidence": {"type": ["string", "null"]},
        "scope": {"type": ["string", "null"]},
        "scope_evidence": {"type": ["string", "null"]},
        "is_fact": {"type": "boolean"},
        "confidence": {"type": "number"},
    },
    # The evidence fields must be required. Left optional, the model simply omits
    # them, and then every field fails grounding and the validator drops a
    # perfectly good label. Asking for the quote is what makes the check possible.
    "required": [
        "metric", "metric_evidence", "unit", "period", "period_evidence",
        "basis", "basis_evidence", "vintage", "vintage_evidence",
        "scope", "scope_evidence", "is_fact", "confidence",
    ],
}

# The model writes these when it means null.
NULLISH = {"", "none", "null", "unknown", "n/a", "na", "not stated", "not specified"}

SYSTEM = """You label a single number that has already been extracted from a financial \
or economic document. You do NOT read the number, change it, or find new numbers. Your \
job is to say what that number measures and under what qualifiers, using ONLY the text \
provided.

Hard rules:
1. Every "*_evidence" field must be copied verbatim from PROVIDED TEXT. If you cannot \
copy a phrase that states it, the field is null. Do not reason it out, do not use \
outside knowledge, do not guess from what is "usual".
2. period, basis, vintage, scope are INDEPENDENT. A missing one is null, never filled \
by inference from the others.
3. Output the JSON keys in the exact order given by the schema. Decide metric first; \
decide is_fact and confidence last.
4. is_fact = false when the number is a page/note reference, a date or year, a \
registration or identifier number, a table-of-contents entry, or the line states no \
measurable fact.

Examples:

PROVIDED TEXT:
  Consolidated Statement of Profit and Loss
  Revenue from operations
  Revenue from operations 8,142 7,225
  (Rs. in crore) Year ended March 31, 2024 Year ended March 31, 2023
CANDIDATE: 8,142
{"metric":"revenue_from_operations","metric_evidence":"Revenue from operations",\
"unit":"INR_crore","period":"year ended March 31, 2024",\
"period_evidence":"Year ended March 31, 2024","basis":"consolidated",\
"basis_evidence":"Consolidated Statement of Profit and Loss","vintage":null,\
"vintage_evidence":null,"scope":null,"scope_evidence":null,"is_fact":true,\
"confidence":0.95}

PROVIDED TEXT:
  Notes to the financial statements (contd.)
  refer note 34 for related party transactions
CANDIDATE: 34
{"metric":null,"metric_evidence":null,"unit":null,"period":null,"period_evidence":null,\
"basis":null,"basis_evidence":null,"vintage":null,"vintage_evidence":null,"scope":null,\
"scope_evidence":null,"is_fact":false,"confidence":0.98}

PROVIDED TEXT:
  Total income 9,001
CANDIDATE: 9,001
{"metric":"total_income","metric_evidence":"Total income","unit":null,"period":null,\
"period_evidence":null,"basis":null,"basis_evidence":null,"vintage":null,\
"vintage_evidence":null,"scope":null,"scope_evidence":null,"is_fact":true,\
"confidence":0.6}"""

# A fiscal year in these filings is stated as its closing date.
FY_END = re.compile(r"(?:march|mar)\s*31,?\s*((?:19|20)\d{2})", re.IGNORECASE)
FY_SHORT = re.compile(r"\bFY\s?(\d{2}|\d{4})\b", re.IGNORECASE)
FY_SPAN = re.compile(r"\b(?:19|20)(\d{2})\s*[-/]\s*(\d{2})\b")
QUARTER = re.compile(r"\bQ([1-4])\b", re.IGNORECASE)

CONSOLIDATED = re.compile(r"\bconsolidated\b", re.IGNORECASE)
STANDALONE = re.compile(r"\bstandalone\b", re.IGNORECASE)


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


def basis_from(section):
    """Consolidation basis from the statement heading, or None."""
    if not section:
        return None
    if CONSOLIDATED.search(section):
        return "consolidated"
    if STANDALONE.search(section):
        return "standalone"
    return None


def _norm(text):
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def provided_text(row):
    """Exactly what the model is shown, and the only thing it may quote."""
    return "\n".join(
        p for p in (row.get("section"), row.get("row_label"), row.get("text"),
                    row.get("context")) if p
    )


def ground(out, source):
    """Drop every field whose supporting quote is not actually in the source.

    This is the part that works. The prompt asks the model to quote; nothing
    stops it inventing a quote too, so each one is checked against the text it
    was shown and the field goes with it when the check fails.
    """
    haystack = _norm(source)
    for field in GROUNDED:
        quote = out.get(f"{field}_evidence")
        if _norm(out.get(field)) in NULLISH:
            out[field] = None
        if not quote or _norm(quote) in NULLISH or _norm(quote) not in haystack:
            out[field] = None
            out[f"{field}_evidence"] = None
    if out.get("basis") not in BASES:
        out["basis"] = None
    if out.get("vintage") not in VINTAGES:
        out["vintage"] = None
    if out.get("unit") not in UNITS:
        out["unit"] = None
    return out


def _prompt(row):
    known_period = period_from(row.get("col_header"))
    known_basis = basis_from(row.get("section"))
    value = " ".join(x for x in (row.get("currency"), row.get("value"), row.get("unit")) if x)

    return (
        f"SUBJECT (given):        {row.get('subject') or 'unknown'}\n"
        f"SECTION HEADER (given): {row.get('section') or 'none'}\n"
        f"KNOWN BASIS (given):    {known_basis or 'unknown - determine from text below'}\n"
        f"KNOWN PERIOD (given):   {known_period or 'unknown - determine from text below'}\n\n"
        f"CANDIDATE NUMBER:       {value or '(none - this is a text excerpt)'}\n"
        f"ROW LABEL (carried):    {row.get('row_label') or 'none'}\n"
        f"SOURCE LINE:            {row['text']}\n"
        f"CONTEXT (3 lines up):\n{row.get('context') or 'none'}\n\n"
        "PROVIDED TEXT = SECTION HEADER + ROW LABEL + SOURCE LINE + CONTEXT. "
        "Quote only from these."
    )


def label_one(row, model=None, timeout=180):
    """Return grounded label fields for one evidence row, or None if the model failed."""
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

    out = ground(out, provided_text(row))

    # Deterministic values win wherever we have them; the model fills gaps only.
    return {
        "evidence_id": row["id"],
        "is_fact": bool(out.get("is_fact")),
        "subject": row.get("subject") or "",  # given by the document, never inferred
        "metric": out.get("metric") or "",
        "metric_evidence": out.get("metric_evidence") or "",
        "period": period_from(row.get("col_header")) or out.get("period") or "",
        "basis": basis_from(row.get("section")) or out.get("basis") or "",
        "basis_evidence": out.get("basis_evidence") or "",
        "vintage": out.get("vintage") or "",
        "scope": out.get("scope") or "",
        "unit": row.get("unit") or out.get("unit") or "",
        "confidence": float(out.get("confidence") or 0.0),
        "model": model or MODEL,
    }


def available():
    """Which models Ollama currently has, so the UI can say what is wrong."""
    try:
        with urllib.request.urlopen(f"{HOST}/api/tags", timeout=3) as resp:
            return [m["name"] for m in json.load(resp).get("models", [])]
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []


if __name__ == "__main__":
    src = "Consolidated Statement of Profit and Loss\nRevenue from operations 8,142 7,225"

    kept = ground({"metric": "revenue_from_operations",
                   "metric_evidence": "Revenue from operations",
                   "basis": "consolidated",
                   "basis_evidence": "Consolidated Statement of Profit and Loss"}, src)
    assert kept["metric"] == "revenue_from_operations", kept
    assert kept["basis"] == "consolidated", kept

    # The failure this exists for: a qualifier nothing in the source states.
    dropped = ground({"vintage": "provisional", "vintage_evidence": "provisional figures"}, src)
    assert dropped["vintage"] is None, dropped

    # An invented quote is still an invented quote.
    faked = ground({"basis": "standalone", "basis_evidence": "Standalone Statement"}, src)
    assert faked["basis"] is None, faked

    # Whitespace and case differences must not fail a real quote.
    loose = ground({"metric": "revenue_from_operations",
                    "metric_evidence": "revenue   from  OPERATIONS"}, src)
    assert loose["metric"] == "revenue_from_operations", loose

    assert period_from("March 31, 2023") == "FY2023"
    assert period_from("Q4 FY24") == "Q4-FY2024"
    assert period_from("Total") is None
    assert basis_from("Consolidated Statement of Profit and Loss") == "consolidated"
    assert basis_from("Revenue from operations") is None
    print("label self-check ok")
