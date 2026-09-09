"""PDF -> grounded facts -> cross-document relations.

Two model passes, nothing else:
  1. per page: pull out facts, each with a verbatim quote we then verify
  2. per candidate pair: decide corroborates / contradicts / reconcilable
Candidate pairs come from cheap token overlap, so the model only judges
pairs that could plausibly be about the same thing.
"""

import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import pymupdf

import db
from llm import ask_json

WORKERS = int(os.getenv("WORKERS", "8"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "20"))      # 0 = every page
MAX_PAIRS = int(os.getenv("MAX_PAIRS", "80"))      # judgement calls per document
PAGE_CHARS = 6000                                  # split longer pages into several calls
MIN_PAGE_CHARS = 200                               # below this a page is furniture or an image

EXTRACT_PROMPT = """You are building a fact knowledge layer from a document.

Extract the factual claims stated in the PAGE TEXT below. Two kinds matter equally:

- NUMERIC facts: amounts, counts, rates, ratios, dates, durations.
- TEXTUAL facts: who holds which role, who was appointed or resigned and when,
  addresses and locations, ownership and subsidiaries, statuses ("active",
  "resigned", "listed", "unaudited"), definitions of a term the document uses,
  policies, obligations, risks, and relationships between named entities.

A page of pure prose with no numbers on it still states facts - extract those.
Do not skip a sentence because it has no figure in it.

Rules:
- "quote" must be copied VERBATIM from the page text, long enough to prove the fact
  and at most 300 characters. Never paraphrase or reconstruct a quote.
- Only what the text states. No inference, no outside knowledge, no arithmetic.
- Use the document's own wording for "subject" and "attribute".
- Skip headers, footers, page numbers, and table-of-contents lines.
- "period" is the time the fact refers to ("FY2024", "as of 31 March 2025", "" if none).
- "scope" is what limits it ("consolidated", "standalone", "express parcel segment",
  "urban areas", "revised estimate", "" if none).
- "unit" is the unit of the value ("INR crore", "%", "days", "" if not numeric).
- A textual fact's "value" is the answer itself: a name, a role, a place, a status,
  a short phrase. Leave "unit" empty for these.

Return ONLY a JSON array, no prose:
[{"subject":"","attribute":"","value":"","unit":"","period":"","scope":"","quote":""}]
Return [] if the page states no facts.

DOCUMENT: {doc}
PAGE {page} TEXT:
\"\"\"
{text}
\"\"\""""

JUDGE_PROMPT = """Two facts were extracted from two different documents. Decide how they relate.

FACT A - from "{a_doc}", page {a_page}
  {a_subject} | {a_attribute} = {a_value} {a_unit}
  period: {a_period} | scope: {a_scope}
  evidence: "{a_quote}"

FACT B - from "{b_doc}", page {b_page}
  {b_subject} | {b_attribute} = {b_value} {b_unit}
  period: {b_period} | scope: {b_scope}
  evidence: "{b_quote}"

Choose one relation:
Values may be numbers or text - a name, a role, a status, an address.

- "corroborates": the same fact about the same thing, same period and scope, and the
  values agree - including when worded differently, rounded, in a different unit, or
  written in a different form (the same address or the same person's name spelled
  differently still agrees).
- "contradicts": the same fact about the same thing, same period and scope, but the
  values cannot both be true - a figure that disagrees, or a person described as
  holding a role in one document and as having left it in the other.
- "reconcilable": they look inconsistent, but a difference in period, scope, unit,
  basis, estimate vintage or definition explains it. Say which difference.
- "unrelated": not the same underlying fact.

Return ONLY JSON:
{{"relation":"...","confidence":0.0,"reasoning":"one or two sentences citing the specific evidence"}}"""

STOPWORDS = {"the", "a", "an", "of", "for", "in", "on", "at", "to", "and", "or", "as",
             "is", "was", "by", "with", "per", "total", "value", "s"}


# ---------------------------------------------------------------- extraction

def read_pages(path, max_pages=MAX_PAGES):
    """[(page_number, text)] - one entry per chunk, long pages split."""
    out = []
    with pymupdf.open(path) as doc:
        pages = doc.page_count if max_pages <= 0 else min(doc.page_count, max_pages)
        for n in range(pages):
            # sorted, so a figure and its label read in the order a human sees them
            text = doc[n].get_text(sort=True).strip()
            if len(text) < MIN_PAGE_CHARS:
                continue
            for i in range(0, len(text), PAGE_CHARS):
                out.append((n + 1, text[i:i + PAGE_CHARS]))
    return out


def _norm(text):
    return re.sub(r"\s+", " ", (text or "")).strip().casefold()


def _alnum(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").casefold())


def check_quote(quote, value, page_text):
    """(is it evidence, what to say about it).

    Three tiers, because a quote can fail to match for three different reasons and
    only one of them means the model made something up.

    1. The quote is on the page, give or take whitespace, case, and the stray
       spaces and broken glyphs PDF extraction leaves behind.
    2. Every word of the quote is on the page, but not in that order. This is what
       a slide looks like: the figure sits in one text box and its label in
       another, and the model reads them together the way a human would. It is
       still evidence, and it is marked so you know the wording was assembled.
    3. Nothing else. A value that is not on the page is a value the model wrote,
       and it is kept out of the knowledge layer.

    A numeric fact also has to have its number inside its own quote. Without that,
    a quote can be a real line of the page and still prove nothing - a column
    heading copied without the figure under it is the usual way this happens."""
    if not quote or len(quote) < 6:
        return False, "quote too short to stand as evidence on its own"
    digits = re.findall(r"\d[\d.,]*", value or "")
    if digits and not any(_alnum(d) in _alnum(quote) for d in digits):
        return False, "quote does not contain the value it is meant to be evidence for"
    if _norm(quote) in _norm(page_text) or _alnum(quote) in _alnum(page_text):
        return True, ""
    words = re.findall(r"[a-z0-9]+", quote.casefold())
    page = _alnum(page_text)
    if len(words) > 1 and all(_alnum(w) in page for w in words):
        return True, "wording assembled from separate blocks on this page"
    return False, "quote is not on this page - the model wrote it instead of copying it"


def extract_page(doc_name, page_no, text):
    prompt = (EXTRACT_PROMPT
              .replace("{doc}", doc_name)
              .replace("{page}", str(page_no))
              .replace("{text}", text))
    facts, err = ask_json(prompt, [])
    if err:
        return [], err
    if not isinstance(facts, list):
        return [], "model did not return a list of facts"
    rows = []
    for f in facts:
        if not isinstance(f, dict) or not str(f.get("value", "")).strip():
            continue
        quote = str(f.get("quote", ""))[:300]
        ok, note = check_quote(quote, str(f.get("value", "")), text)
        rows.append((
            str(f.get("subject", ""))[:200], str(f.get("attribute", ""))[:200],
            str(f.get("value", ""))[:200], str(f.get("unit", ""))[:60],
            str(f.get("period", ""))[:80], str(f.get("scope", ""))[:200],
            quote, page_no, int(ok), note,
        ))
    return rows, None


# ------------------------------------------------------------------ linking

def tokens(fact):
    words = re.findall(r"[a-z0-9]+", f"{fact['subject']} {fact['attribute']}".casefold())
    return {w for w in words if w not in STOPWORDS and len(w) > 1}


def candidates(new_facts, old_facts, per_fact=4):
    """(pairs worth judging, how many were left unjudged).

    Cheap blocking: token overlap between subject+attribute. Pairs are ranked by
    that overlap and cut at MAX_PAIRS, and the number cut is returned rather than
    dropped quietly - a pair the model never saw is not the same as a pair it
    found unrelated, and only one of those is a statement about the documents.

    ponytail: Jaccard on words, so "topline" and "revenue" never meet.
    Swap in embeddings if recall matters more than having zero dependencies."""
    old = [(f, tokens(f)) for f in old_facts]
    pairs = []
    for fact in new_facts:
        mine = tokens(fact)
        if not mine:
            continue
        scored = []
        for other, theirs in old:
            union = mine | theirs
            score = len(mine & theirs) / len(union) if union else 0
            if score >= 0.34:
                scored.append((score, other))
        scored.sort(key=lambda s: -s[0])
        pairs += [(score, fact, other) for score, other in scored[:per_fact]]
    pairs.sort(key=lambda p: -p[0])
    return [(a, b) for _, a, b in pairs[:MAX_PAIRS]], max(0, len(pairs) - MAX_PAIRS)


def judge(pair):
    a, b = pair
    prompt = JUDGE_PROMPT.format(
        **{f"a_{k}": a[k] for k in
           ("doc", "page", "subject", "attribute", "value", "unit", "period", "scope", "quote")},
        **{f"b_{k}": b[k] for k in
           ("doc", "page", "subject", "attribute", "value", "unit", "period", "scope", "quote")},
    )
    verdict, err = ask_json(prompt, {})
    if err:
        return None, err
    if not isinstance(verdict, dict):
        return None, "model did not return a verdict object"
    relation = str(verdict.get("relation", "")).lower().strip()
    if relation not in ("corroborates", "contradicts", "reconcilable"):
        return None, None      # "unrelated", and anything unrecognised, is not stored
    try:
        confidence = min(1.0, max(0.0, float(verdict.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5
    return (a["id"], b["id"], relation, confidence,
            str(verdict.get("reasoning", ""))[:1000]), None


# ------------------------------------------------------------------- driver

def process(doc_id, max_pages=MAX_PAGES):
    """Run one document end to end. Existing documents are never re-processed;
    a new one only has to be compared against what is already stored."""
    doc = db.rows("SELECT * FROM documents WHERE id=?", (doc_id,))[0]
    try:
        chunks = read_pages(doc["path"], max_pages)
        if not chunks:
            raise ValueError("no extractable text - is this a scanned PDF?")
        db.write("UPDATE documents SET status='extracting', pages=? WHERE id=?",
                 (len({c[0] for c in chunks}), doc_id))

        # Counted as they land, because the only thing worse than a slow document
        # is a slow document that looks identical to a stuck one.
        with ThreadPoolExecutor(WORKERS) as pool:
            futures = [pool.submit(extract_page, doc["name"], page, text)
                       for page, text in chunks]
            results = []
            for done in as_completed(futures):
                results.append(done.result())
                db.write("UPDATE documents SET status=? WHERE id=?",
                         (f"extracting {len(results)}/{len(chunks)}", doc_id))
        rows = [r for batch, _ in results for r in batch]
        failures = [err for _, err in results if err]
        with db.connect() as conn:
            conn.executemany(
                "INSERT INTO facts (subject,attribute,value,unit,period,scope,quote,page,"
                "grounded,note,doc_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [r + (doc_id,) for r in rows])

        db.write("UPDATE documents SET status='linking' WHERE id=?", (doc_id,))

        # A model call that failed is not the same as a page with no facts, and a
        # pair that was never compared is not the same as one found unrelated.
        # Both are invisible unless the document says so.
        notes = ([f"{len(failures)} of {len(chunks)} page calls failed - "
                  f"first: {failures[0]}"] if failures else []) + link(doc_id)
        note = "; ".join(notes)
        db.write("UPDATE documents SET status='done', note=? WHERE id=?", (note[:400], doc_id))
    except Exception as exc:
        db.write("UPDATE documents SET status='failed', note=? WHERE id=?",
                 (str(exc)[:400], doc_id))
        raise


def link(doc_id):
    sql = ("SELECT f.*, d.name AS doc FROM facts f JOIN documents d ON d.id=f.doc_id "
           "WHERE f.grounded=1 AND f.doc_id{} ?")
    new_facts = db.rows(sql.format("="), (doc_id,))
    old_facts = db.rows(sql.format("!="), (doc_id,))
    pairs, unjudged = candidates(new_facts, old_facts)
    if not pairs:
        return []
    with ThreadPoolExecutor(WORKERS) as pool:
        judged = list(pool.map(judge, pairs))
    found = [row for row, _ in judged if row]
    with db.connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO relations (a_id,b_id,relation,confidence,reasoning) "
            "VALUES (?,?,?,?,?)", found)
    errors = [err for _, err in judged if err]
    if unjudged:
        errors.append(f"{unjudged} candidate pairs went unjudged - raise MAX_PAIRS "
                      f"(currently {MAX_PAIRS}) to compare them")
    return errors
