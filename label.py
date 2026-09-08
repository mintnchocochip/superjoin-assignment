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
# One call carries a section header, a row label, a source line and three lines of
# context - a few hundred tokens, so 4096 was never the binding constraint on
# quality. It is the binding constraint on how much of a GPU a run can use: the
# KV cache is num_ctx x however many requests Ollama serves in parallel, so a card
# holding a 5GB model and nothing else stays idle. Raise this and OLLAMA_NUM_PARALLEL
# together; a bigger OLLAMA_MODEL is the other way to spend the same VRAM.
NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "16384"))

# Ollama unloads after five minutes by default, and reloading the model between
# documents costs more than the labelling does.
KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")

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
#
# metric_key is an enum and metric is free text, on purpose. Grouping keys off the
# enum, so "Revenue from operations", "Total income" and "Revenue" reach the same
# group instead of three groups of one - which is what left corroboration finding
# almost nothing. metric keeps the document's own wording for display.
def _schema(metric_keys):
    return {
        "type": "object",
        "properties": {
            "metric": {"type": ["string", "null"]},
            "metric_evidence": {"type": ["string", "null"]},
            "metric_key": {"type": ["string", "null"], "enum": list(metric_keys) + [None]},
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
            "metric", "metric_evidence", "metric_key", "unit", "period", "period_evidence",
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
2. metric is the row's own wording. metric_key is the bucket it belongs to, chosen \
from the fixed list the schema allows - it is what lets this figure be compared with \
the same figure in another document, so choose the bucket that matches what the row \
measures, "other" when it measures something real that no bucket fits, and null only \
when metric itself is null.
3. period, basis, vintage, scope are INDEPENDENT. A missing one is null, never filled \
by inference from the others.
4. Output the JSON keys in the exact order given by the schema. Decide metric first; \
decide is_fact and confidence last.
5. is_fact = false when the number is a page/note reference, a date or year, a \
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
"metric_key":"revenue","unit":"INR_crore","period":"year ended March 31, 2024",\
"period_evidence":"Year ended March 31, 2024","basis":"consolidated",\
"basis_evidence":"Consolidated Statement of Profit and Loss","vintage":null,\
"vintage_evidence":null,"scope":null,"scope_evidence":null,"is_fact":true,\
"confidence":0.95}

PROVIDED TEXT:
  Notes to the financial statements (contd.)
  refer note 34 for related party transactions
CANDIDATE: 34
{"metric":null,"metric_evidence":null,"metric_key":null,"unit":null,"period":null,\
"period_evidence":null,"basis":null,"basis_evidence":null,"vintage":null,\
"vintage_evidence":null,"scope":null,"scope_evidence":null,"is_fact":false,\
"confidence":0.98}

PROVIDED TEXT:
  Total income 9,001
CANDIDATE: 9,001
{"metric":"total_income","metric_evidence":"Total income","metric_key":"revenue",\
"unit":null,"period":null,"period_evidence":null,"basis":null,"basis_evidence":null,"vintage":null,\
"vintage_evidence":null,"scope":null,"scope_evidence":null,"is_fact":true,\
"confidence":0.6}"""

# Prose gets its own instructions. Handed the numeric SYSTEM above, a text row
# arrives as "CANDIDATE NUMBER: (none)" and the model does the consistent thing
# with a prompt that opens "You label a single number": it returns is_fact false
# and every field null. The mining already yields these rows and worth_labelling
# already keeps them; only the instruction was missing.
TEXT_SYSTEM = """You label a single sentence that has already been extracted from a \
financial or economic document. You do NOT summarise the document or add anything it \
does not say. Your job is to say what that sentence asserts about the SUBJECT, using \
ONLY the text provided.

Hard rules:
1. Every "*_evidence" field must be copied verbatim from PROVIDED TEXT. If you cannot \
copy a phrase that states it, the field is null. Do not reason it out, do not use \
outside knowledge, do not guess from what is "usual".
2. metric names WHAT IS ASSERTED, in snake_case, from the sentence's own words: \
director_appointment, volume_growth_guidance, acquisition, dividend_declared, \
capacity_expansion. Not a summary of the sentence, and never a metric the sentence \
does not state.
3. metric_key is the kind of event that is, chosen from the fixed list the schema \
allows - it is what lets this sentence be read alongside the same kind of statement \
elsewhere. "other" when the sentence asserts something real that no listed kind fits, \
null only when metric itself is null.
4. period, basis, vintage, scope are INDEPENDENT. A missing one is null, never filled \
by inference from the others. unit is null here - a sentence carries no number.
5. Output the JSON keys in the exact order given by the schema. Decide metric first; \
decide is_fact and confidence last.
6. is_fact = false when the sentence asserts nothing checkable about the subject: a \
heading, a table-of-contents line, boilerplate, a definition, a page footer, or a \
statement about someone other than the subject.

Examples:

PROVIDED TEXT:
  Directors' Report
  Mr. Sandeep Kumar Barasia was appointed as Executive Director and Chief Business \
Officer of the Company with effect from May 20, 2022.
SUBJECT: Delhivery Limited
{"metric":"director_appointment","metric_evidence":"was appointed as Executive \
Director and Chief Business Officer","metric_key":"appointment","unit":null,\
"period":"May 20, 2022",\
"period_evidence":"with effect from May 20, 2022","basis":null,"basis_evidence":null,\
"vintage":null,"vintage_evidence":null,"scope":"Executive Director and Chief Business \
Officer","scope_evidence":"Executive Director and Chief Business Officer",\
"is_fact":true,"confidence":0.9}

PROVIDED TEXT:
  Notes to the financial statements
  The accompanying notes form an integral part of these financial statements.
SUBJECT: Delhivery Limited
{"metric":null,"metric_evidence":null,"metric_key":null,"unit":null,"period":null,\
"period_evidence":null,"basis":null,"basis_evidence":null,"vintage":null,\
"vintage_evidence":null,"scope":null,"scope_evidence":null,"is_fact":false,\
"confidence":0.95}"""

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

# What a sentence can be about. The numeric buckets are metrics; these are events,
# and they exist for the same reason: two documents describing the same kind of
# thing about one company have to land in one group to be read against each other.
EVENT_KEYS = [
    "appointment", "departure", "acquisition", "divestment", "guidance",
    "dividend", "capacity_expansion", "launch", "approval", "policy_change",
    "other",
]

METRIC_KEYS = sorted(TARGETS) + ["other"]
SCHEMA = _schema(METRIC_KEYS)
TEXT_SCHEMA = _schema(EVENT_KEYS)

# Prose is worth labelling only when something happened to somebody. Everything
# else on these pages is disclaimer, boilerplate and navigation.
EVENTS = (
    "appointed", "re-appointed", "resigned", "retired", "ceased to be", "stepped down",
    "acquired", "acquisition", "divested", "merged", "amalgamat",
    "guidance", "expects", "expected to", "outlook", "projected", "forecast",
    "approved", "declared", "commissioned", "launched", "stake in",
)

# The whitelist above is a spend decision, not a correctness one. It exists because
# forty seconds of model time on a figure nothing can be compared against is forty
# seconds wasted - which stops being true on a card with headroom to spare.
# LABEL_ALL=1 labels every mined row that names anything, listed or not.
LABEL_ALL = os.environ.get("LABEL_ALL", "").lower() not in ("", "0", "false", "no")

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
        if target_metric(row.get("row_label")) is not None:
            return True
        # Still needs a label: a figure with nothing naming it is a page number.
        return LABEL_ALL and bool(_bare(row.get("row_label")))
    bare = _bare(row.get("text"))
    if not bare:
        return False
    if LABEL_ALL or any(verb in bare for verb in EVENTS):
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
    if row.get("kind") == "text":
        return (
            f"SUBJECT (given):        {row.get('subject') or 'unknown'}\n"
            f"SECTION HEADER (given): {row.get('section') or 'none'}\n\n"
            f"SENTENCE:               {row['text']}\n"
            f"CONTEXT (3 lines up):\n{row.get('context') or 'none'}\n\n"
            "PROVIDED TEXT = SECTION HEADER + SENTENCE + CONTEXT. Quote only from these."
        )

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
            "keep_alive": KEEP_ALIVE,
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


def metric_key_for(row, out):
    """The bucket this claim groups under - the thing that decides what it can be
    compared with. Deterministic where the row label settles it, the model's enum
    next, its own wording last.

    "other" is deliberately not a bucket. Every unlisted figure would land in one
    group and adjudicate against everything else in it, which is worse than not
    grouping at all; falling through to the wording at least puts two rows that
    say the same thing together.
    """
    if row.get("kind") != "text" and (key := target_metric(row.get("row_label"))):
        return key
    key = _norm(out.get("metric_key"))
    if key and key not in NULLISH and key != "other":
        return key
    return _bare(out.get("metric")).replace(" ", "_")


def label_one(row, model=None, timeout=180):
    """Return grounded label fields for one evidence row, or None if the model failed."""
    text_row = row.get("kind") == "text"
    system, schema = (TEXT_SYSTEM, TEXT_SCHEMA) if text_row else (SYSTEM, SCHEMA)
    out, err = _generate(_prompt(row), model, timeout, system=system, schema=schema)
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
        "metric_key": metric_key_for(row, out),
        # Canonical on both paths. A column header gives FY2024 and the model, told
        # to quote, gives "year ended March 31, 2024" - and two claims stating the
        # same period in two spellings read as a difference, which turns a
        # corroboration into a "reconcilable: one covers X, the other Y".
        "period": (period_from(row.get("col_header")) or period_from(out.get("period"))
                   or out.get("period") or ""),
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
    # The model quotes the page; the header is parsed. Both must land on one
    # spelling or a corroboration reads as a difference in period.
    assert period_from("year ended March 31, 2024") == period_from("March 31, 2024")
    assert period_from("FY2024") == "FY2024"
    assert basis_from("Consolidated Statement of Profit and Loss") == "consolidated"
    assert basis_from("Revenue from operations") is None

    # A column band is more specific than the statement heading above it. This is
    # what separates two figures that a single heading cannot tell apart.
    heading = "Consolidated Statement of Profit and Loss"
    assert known_basis({"section": heading, "col_basis": "Standalone"}) == "standalone"
    assert known_basis({"section": heading, "col_basis": None}) == "consolidated"
    assert known_basis({"section": None, "col_basis": None}) is None

    # The group key. A row label the whitelist recognises settles it without the
    # model; "revenue from operations" and "total income" are one bucket, which is
    # the whole point - three groups of one corroborate nothing.
    number = {"kind": "number", "row_label": "Revenue from operations"}
    assert metric_key_for(number, {"metric_key": "expenses"}) == "revenue"
    assert metric_key_for({"kind": "number", "row_label": "Total income"},
                          {"metric_key": None}) == "revenue"

    # Unlisted label: the model's bucket, then its own wording. Never "other" -
    # that would pile every unrelated figure into one group and adjudicate them
    # against each other.
    unlisted = {"kind": "number", "row_label": "Rubber waste"}
    assert metric_key_for(unlisted, {"metric_key": "volume"}) == "volume"
    assert metric_key_for(unlisted, {"metric_key": "other",
                                     "metric": "Rubber waste sold"}) == "rubber_waste_sold"
    assert metric_key_for(unlisted, {"metric_key": None, "metric": None}) == ""

    # Prose never consults the numeric whitelist - it has no row label to match.
    assert metric_key_for({"kind": "text"}, {"metric_key": "appointment"}) == "appointment"

    # LABEL_ALL widens the prefilter and nothing else: an unlisted row label is
    # labelled, a figure with no label at all is still skipped.
    globals()["LABEL_ALL"] = True
    assert worth_labelling({"accepted": True, "kind": "number", "row_label": "Rubber waste"})
    assert not worth_labelling({"accepted": True, "kind": "number", "row_label": None})
    assert worth_labelling(
        {"accepted": True, "kind": "text", "text": "This presentation is for information"})
    assert not worth_labelling({"accepted": False, "kind": "number", "row_label": "Total income"})
    globals()["LABEL_ALL"] = False

    # Prose and figures take different instructions; a text row must not be asked
    # to label a number that is not there.
    assert "single sentence" in TEXT_SYSTEM
    assert "CANDIDATE NUMBER" not in _prompt({"kind": "text", "text": "x", "subject": "y"})
    assert "CANDIDATE NUMBER" in _prompt({"kind": "number", "text": "x", "value": "1"})
    print("label self-check ok")

    # The half that needs a model. Skipped when Ollama is not up, so the offline
    # check above still runs anywhere; on the GPU box this is the one that proves
    # a sentence comes back labelled rather than blank.
    if MODEL in available():
        prose = label_one({
            "id": "selfcheck", "kind": "text", "subject": "Delhivery Limited",
            "section": "Directors' Report", "row_label": None, "col_header": None,
            "value": None, "currency": "", "unit": "",
            "text": "Mr. Sandeep Kumar Barasia was appointed as Executive Director and "
                    "Chief Business Officer of the Company with effect from May 20, 2022.",
            "context": "Changes in Directors and Key Managerial Personnel",
        }, timeout=300)
        assert prose, "the model failed outright - run probe() for the reason"
        assert prose["is_fact"], prose
        assert prose["metric"], prose
        print(f"live text check ok: {prose['metric']} <- {prose['metric_evidence']!r}")
    else:
        print(f"live text check skipped: {MODEL} not loaded at {HOST}")
