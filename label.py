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

import difflib
import json
import os
import re
import sys
import urllib.error
import urllib.request

def _host(raw):
    """Normalise OLLAMA_HOST, which Ollama itself writes as bare `host:port`.

    Worth supporting both spellings because the default port often cannot be
    used on Windows: 11434 falls inside a reserved TCP range that Hyper-V claims
    (`netsh interface ipv4 show excludedportrange protocol=tcp`), so Ollama
    fails to bind with "an attempt was made to access a socket in a way
    forbidden by its access permissions" once Docker Desktop has started. Moving
    it needs one variable that both processes understand.
    """
    raw = (raw or "").strip().rstrip("/")
    if not raw:
        return "http://127.0.0.1:11434"
    return raw if raw.startswith(("http://", "https://")) else f"http://{raw}"


HOST = _host(os.environ.get("OLLAMA_HOST"))
MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:4b")
NUM_CTX = 4096

GROUNDED = ("metric", "period", "basis", "vintage", "scope")
IDENTITY = ("subject", "doc_type")

# What kind of document this is. Recorded, and deliberately NOT part of a group
# key - an annual report figure and an earnings deck figure for the same metric
# have to stay comparable. Its use is the reverse comparison: same metric, same
# doc_type, different subject, which is how one company gets read against
# another. That query is not wired yet.
DOC_TYPES = {
    "annual_report", "prospectus", "earnings_presentation", "press_release",
    "economic_survey", "central_bank_report", "imf_report", "other",
}
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


def basis_from(text):
    """Consolidation basis stated in a heading or a column band, or None."""
    if not text:
        return None
    if CONSOLIDATED.search(text):
        return "consolidated"
    if STANDALONE.search(text):
        return "standalone"
    return None


def known_basis(row):
    """The basis for one figure, most specific source first.

    A column band beats the statement heading, because a statement carrying both
    consolidated and standalone columns has one heading and two answers - and
    those columns are exactly the figures that otherwise look like a
    contradiction. The model only gets a say when the page states neither.
    """
    return basis_from(row.get("col_basis")) or basis_from(row.get("section"))


# Only evidence that could end up in a finding is worth 40 seconds of model time.
# A finding needs two claims about the same metric to compare, so a figure whose
# row label names nothing on this list can never produce one - it is stored, and
# simply not labelled.
TARGETS = {
    "revenue": ("revenue", "revenue from operations", "total income", "net sales", "turnover"),
    "profit": ("profit for the year", "net profit", "profit after tax", "pat",
               "loss for the year", "profit before tax"),
    "ebitda": ("ebitda", "operating profit", "adjusted ebitda"),
    "expenses": ("total expenses", "total expenditure", "cost of services", "finance costs"),
    "assets": ("total assets", "non-current assets", "current assets"),
    "equity": ("total equity", "total liabilities", "net worth", "shareholders funds"),
    "eps": ("earnings per share", "basic eps", "diluted eps"),
    "cash": ("cash and cash equivalents", "free cash flow", "net cash"),
    "margin": ("gross margin", "operating margin", "ebitda margin"),
    "capex": ("capital expenditure", "capex", "additions to property"),
    "borrowings": ("borrowings", "lease liabilities", "total debt"),
    "tax": ("tax expense", "current tax", "deferred tax"),
    "volume": ("shipments", "tonnage", "parcels", "freight volume", "orders"),
    "employees": ("employee benefits expense", "headcount", "number of employees"),
    "gdp": ("gdp growth", "real gdp", "gross domestic product", "gross value added"),
    "inflation": ("inflation", "consumer price index", "wholesale price index",
                  "headline inflation", "core inflation"),
    "fiscal": ("fiscal deficit", "current account deficit", "revenue deficit",
               "primary deficit"),
    "trade": ("exports", "imports", "trade deficit", "trade balance"),
    "reserves": ("foreign exchange reserves", "forex reserves"),
    "credit": ("bank credit", "credit growth", "repo rate", "policy rate"),
}
TERMS = tuple(sorted({t for group in TARGETS.values() for t in group}, key=len, reverse=True))

# Prose is worth labelling only when something happened to somebody. Everything
# else on these pages is disclaimer, boilerplate and navigation.
EVENTS = (
    "appointed", "re-appointed", "resigned", "retired", "ceased to be", "stepped down",
    "acquired", "acquisition", "divested", "merged", "amalgamat",
    "guidance", "expects", "expected to", "outlook", "projected", "forecast",
    "approved", "declared", "commissioned", "launched", "stake in",
)

NOISE = re.compile(r"[^a-z ]+")


def _norm(text):
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _bare(text):
    """Lowercase words only - figures and punctuation stripped, so a row label
    matches its target term regardless of the numbers sitting next to it."""
    return re.sub(r"\s+", " ", NOISE.sub(" ", _norm(text))).strip()


def target_metric(row_label, cutoff=0.86):
    """The target group this row label belongs to, or None.

    Substring first because it settles most rows for nothing. difflib only sees
    what survives, which is where the near-misses live: hyphenation, an inserted
    word, a trailing '(contd.)'.
    """
    bare = _bare(row_label)
    if not bare:
        return None
    for group, terms in TARGETS.items():
        if any(term in bare for term in terms):
            return group
    close = difflib.get_close_matches(bare, TERMS, n=1, cutoff=cutoff)
    if close:
        return next(g for g, terms in TARGETS.items() if close[0] in terms)
    return None


def worth_labelling(row, subject=""):
    """Whether this evidence could ever appear in a finding."""
    if not row.get("accepted"):
        return False
    if row["kind"] == "number":
        return target_metric(row.get("row_label")) is not None
    bare = _bare(row.get("text"))
    if not bare:
        return False
    if any(verb in bare for verb in EVENTS):
        return True
    # A subject named in prose is worth a look even without an event verb.
    words = [w for w in _bare(subject).split() if len(w) > 3]
    return bool(words) and any(w in bare for w in words)


def provided_text(row):
    """Exactly what the model is shown, and the only thing it may quote."""
    return "\n".join(
        p for p in (row.get("section"), row.get("row_label"), row.get("text"),
                    row.get("context")) if p
    )


def ground(out, source, fields=GROUNDED):
    """Drop every field whose supporting quote is not actually in the source.

    This is the part that works. The prompt asks the model to quote; nothing
    stops it inventing a quote too, so each one is checked against the text it
    was shown and the field goes with it when the check fails.
    """
    haystack = _norm(source)
    for field in fields:
        quote = out.get(f"{field}_evidence")
        if _norm(out.get(field)) in NULLISH:
            out[field] = None
        if not quote or _norm(quote) in NULLISH or _norm(quote) not in haystack:
            out[field] = None
            out[f"{field}_evidence"] = None
    for field, allowed in (("basis", BASES), ("vintage", VINTAGES),
                           ("unit", UNITS), ("doc_type", DOC_TYPES)):
        if field in out and out[field] not in allowed:
            out[field] = None
    return out


def _prompt(row):
    known_period = period_from(row.get("col_header"))
    known = known_basis(row)
    value = " ".join(x for x in (row.get("currency"), row.get("value"), row.get("unit")) if x)

    return (
        f"SUBJECT (given):        {row.get('subject') or 'unknown'}\n"
        f"SECTION HEADER (given): {row.get('section') or 'none'}\n"
        f"KNOWN BASIS (given):    {known or 'unknown - determine from text below'}\n"
        f"KNOWN PERIOD (given):   {known_period or 'unknown - determine from text below'}\n\n"
        f"CANDIDATE NUMBER:       {value or '(none - this is a text excerpt)'}\n"
        f"ROW LABEL (carried):    {row.get('row_label') or 'none'}\n"
        f"SOURCE LINE:            {row['text']}\n"
        f"CONTEXT (3 lines up):\n{row.get('context') or 'none'}\n\n"
        "PROVIDED TEXT = SECTION HEADER + ROW LABEL + SOURCE LINE + CONTEXT. "
        "Quote only from these."
    )


def _generate(prompt, model=None, timeout=180, system=None, schema=None):
    """One constrained generation. Returns (parsed, error); exactly one is None."""
    body = json.dumps(
        {
            "model": model or MODEL,
            "system": system or SYSTEM,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "format": schema or SCHEMA,
            "options": {"temperature": 0, "num_ctx": NUM_CTX},
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{HOST}/api/generate", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(json.load(resp)["response"]), None
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}"
    except (urllib.error.URLError, TimeoutError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    except (json.JSONDecodeError, KeyError) as exc:
        return None, f"unparseable response: {type(exc).__name__}: {exc}"


IDENTIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": ["string", "null"]},
        "subject_evidence": {"type": ["string", "null"]},
        "doc_type": {"type": ["string", "null"], "enum": sorted(DOC_TYPES) + [None]},
        "doc_type_evidence": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
    },
    "required": ["subject", "subject_evidence", "doc_type", "doc_type_evidence",
                 "confidence"],
}

IDENTIFY_SYSTEM = """You are shown the opening pages of a financial or economic document. \
Say which entity it is ABOUT and what kind of document it is. Use ONLY the text provided.

subject   the organisation or economy the document reports on, as the document
          writes it: "Delhivery Limited", "India". Not the publisher when they
          differ - an IMF staff report on India has subject India, not the IMF.
subject_evidence
          a phrase copied verbatim from the text that names it. If you cannot
          copy one, both fields are null.
doc_type  one of: annual_report, prospectus, earnings_presentation,
          press_release, economic_survey, central_bank_report, imf_report, other
doc_type_evidence
          a phrase copied verbatim that shows what kind of document this is,
          e.g. a title. Null if you cannot copy one.
confidence 0.0-1.0, decided last.

Never infer from what is usual. If the text does not say it, the field is null."""


def identify(cover, model=None, timeout=120):
    """Propose what a document is about and what kind it is, from its cover pages.

    Grounded the same way labels are: every field arrives with a quote, and a
    quote that is not in the cover text takes its field with it. A document that
    never names its subject produces None rather than a guess, which is the
    behaviour that keeps a misfiled document visible instead of silently
    creating a corpus of one.
    """
    out, err = _generate(
        f"OPENING PAGES:\n{cover}\n\nQuote only from the text above.",
        model=model, timeout=timeout,
        system=IDENTIFY_SYSTEM, schema=IDENTIFY_SCHEMA,
    )
    if err:
        print(f"identify failed: {err}", file=sys.stderr)
        return None

    out = ground(out, cover, fields=IDENTITY)
    return {
        "subject": out.get("subject") or "",
        "subject_evidence": out.get("subject_evidence") or "",
        "doc_type": out.get("doc_type") or "other",
        "doc_type_evidence": out.get("doc_type_evidence") or "",
        "confidence": float(out.get("confidence") or 0.0),
        "model": model or MODEL,
    }


def probe(model=None):
    """One real generation, to fail fast with the actual reason.

    /api/tags answering does not mean the model loads. A corrupt or unloadable
    blob returns HTTP 500 per request, and a labelling run that swallows that
    looks exactly like a labelling run that is merely slow - which cost an hour
    of watching a counter sit at zero.
    """
    _, err = _generate("SOURCE LINE: Total income 1\nCANDIDATE NUMBER: 1", model, timeout=60)
    return err


def label_one(row, model=None, timeout=180):
    """Return grounded label fields for one evidence row, or None if the model failed."""
    out, err = _generate(_prompt(row), model, timeout)
    if err:
        print(f"label_one failed on {row.get('id')}: {err}", file=sys.stderr)
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
        "basis": known_basis(row) or out.get("basis") or "",
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

    assert target_metric("Revenue from Operations") == "revenue"
    assert target_metric("Profit for the year 8,142") == "profit"
    assert target_metric("Total assets") == "assets"
    assert target_metric("Gross domestic product at constant prices") == "gdp"
    assert target_metric("Peer Review Certificate No.") is None
    assert target_metric("Rubber waste") is None
    assert target_metric(None) is None

    assert worth_labelling({"accepted": True, "kind": "number", "row_label": "Total income"})
    assert not worth_labelling({"accepted": True, "kind": "number", "row_label": "Rubber waste"})
    assert not worth_labelling({"accepted": False, "kind": "number", "row_label": "Total income"})
    assert worth_labelling(
        {"accepted": True, "kind": "text", "text": "Mr X was appointed as director"})
    assert not worth_labelling(
        {"accepted": True, "kind": "text", "text": "This presentation is for information"})
    assert worth_labelling(
        {"accepted": True, "kind": "text", "text": "Delhivery operates 90 gateways"},
        subject="delhivery limited")

    # Identity grounding: same rule, different fields.
    cover = "Delhivery Limited\nAnnual Report 2023-24\nConsolidated Financial Statements"
    kept = ground({"subject": "Delhivery Limited", "subject_evidence": "Delhivery Limited",
                   "doc_type": "annual_report", "doc_type_evidence": "Annual Report 2023-24"},
                  cover, fields=IDENTITY)
    assert kept["subject"] == "Delhivery Limited", kept
    assert kept["doc_type"] == "annual_report", kept

    # An entity the cover never names must not survive.
    invented = ground({"subject": "DHL Group", "subject_evidence": "DHL Group"},
                      cover, fields=IDENTITY)
    assert invented["subject"] is None, invented

    # A doc_type outside the vocabulary is dropped even when quoted.
    bogus = ground({"doc_type": "quarterly_thing", "doc_type_evidence": "Annual Report 2023-24"},
                   cover, fields=IDENTITY)
    assert bogus["doc_type"] is None, bogus

    assert period_from("March 31, 2023") == "FY2023"
    assert period_from("Q4 FY24") == "Q4-FY2024"
    assert period_from("Total") is None
    assert basis_from("Consolidated Statement of Profit and Loss") == "consolidated"
    assert basis_from("Revenue from operations") is None

    # A column band is more specific than the statement heading above it. This is
    # what separates two figures that a single heading cannot tell apart.
    heading = "Consolidated Statement of Profit and Loss"
    assert known_basis({"section": heading, "col_basis": "Standalone"}) == "standalone"
    assert known_basis({"section": heading, "col_basis": None}) == "consolidated"
    assert known_basis({"section": None, "col_basis": None}) is None
    print("label self-check ok")
