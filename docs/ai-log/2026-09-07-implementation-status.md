# Implementation status

Session `2f9f40b0` · 7 turns

## Turn 1 — 2026-09-07 16:45

**Me:**

> what has been implemented till now?

*[Claude ran 6 tools: Read ×5, Glob ×1]*

**Claude:**

Current state: **phase 1 of the pipeline only — regex evidence mining and an inspection UI.** Everything downstream of that is designed (in the [README](README.md) Approach section) but not built.

## Implemented

**[extract.py](extract.py)** — deterministic number mining, no model
- `mine()` walks a PDF with PyMuPDF, regex-matches numbers (Indian + international digit grouping, currency markers, magnitude words)
- Label carry-forward for table cells (figure and row-label land on separate lines)
- `_reject_reason()` heuristics filter out bare years, folios, cross-references (`note 3`), registration numbers (DIN/CIN/PAN…), unqualified small numbers — rejects are *kept* with their reason for tuning
- `_printed_page()` — last-lines heuristic to capture printed folio vs PDF index (marked `ponytail:` as a known corner)
- Char offsets into the stored line so the UI can highlight the exact number

**[app.py](app.py)** — FastAPI + **SQLite** (`facts.db`), not MongoDB
- `POST /api/documents` — upload, sha256 dedup, synchronous mining, store `documents` + `candidates`
- `GET /api/documents` — list with accepted/rejected counts
- `GET /api/documents/{id}/candidates` — filter by accepted flag, text query, page
- `GET /api/documents/{id}/rejections` — reject reasons by frequency
- `DELETE /api/documents/{id}`

**[static/index.html](static/index.html)** — single-page UI: drag-drop upload, document list, evidence table with number highlighting, "show rejected" toggle, text/page filters. Header literally says "regex only, no model in the loop yet".

**[docs/ai-log/](docs/ai-log/)** + [tools/export_transcript.py](tools/export_transcript.py) + Stop hook — auto-generated AI transparency log.

## Not implemented (designed only)

- **Steps 4–8 of the pipeline**: SLM labelling (Ollama `qwen2.5:7b`), unit/period normalization, embedding-based metric aliasing (`nomic-embed-text`), claim assembly
- **Adjudication**: claim groups, pairwise verdict logic (corroborates / contradicts / reconcilable), `verdicts[]`
- Endpoints `GET /claims`, `GET /groups/{key}`, `GET /findings`
- MongoDB migration (SQLite is explicitly a placeholder)
- The **4 required assignment cases**, setup instructions, demo video, Limitations — all `_TODO_` in the README

So: the "value never comes from a model" foundation is in place and inspectable; the fact/claim/verdict layer that the assignment actually grades is still ahead.

---

## Turn 2 — 2026-09-07 16:52

**Me:**

> does qwen 3b or gemma work well enough to extract facts and evidence from the pdf and distringuish from the noise

**Claude:**

Short answer: **yes for the job the README actually assigns the model, with two real caveats.** The design already does the smart thing — it doesn't ask the model to "extract facts," it asks it to *label a number that regex already found*. That reframing is what makes a 3B viable. If you were asking the model to read a page and emit facts freeform, no, a 3B/4B would produce noise.

## The distinction that matters

Two different jobs, and only one of them should touch a model:

| Job | Do it with | 3B/Gemma verdict |
|---|---|---|
| Is this number noise (folio, note ref, DIN, date)? | regex `_reject_reason()` — already built | model would be *worse and slower* here |
| What metric/period/basis is this number? | local SLM, schema-constrained | good enough, with caveats below |
| Which canonical metric name? | embeddings, separate step | don't make the 3B do this |
| Is the quote verbatim? | it's the mined line, by construction | model never produces it |

Keep noise discrimination deterministic. A small model as a *second-stage* binary filter on the already-accepted set ("does this line state a financial/economic fact? y/n") is cheap and reliable — that's a genuinely easy call for a 3B. Multi-field extraction is where it gets shaky.

## Where 3B / Gemma actually break

1. **Column→period alignment.** Tables read `FY24 | FY23` across the top; the model has to know `8,142` is the FY24 column. PyMuPDF has already flattened the 2D layout by the time the model sees it, so this is guesswork. This is the #1 accuracy risk and it's **not** a model-size problem — a 7B fails it too. Fix it by carrying column headers and section headers forward the same way [extract.py:96](extract.py:96) already carries row labels forward.
2. **Basis (consolidated vs standalone) and scope** ("excluding exceptional items", "restated") — usually stated once in a heading 15–30 lines up, outside the context window you feed. Same fix: propagate section headers.

Give the schema an abstain path (`metric: null` / low confidence) so ambiguous lines don't become claims instead of becoming wrong claims.

## Model choice

- **Qwen2.5-3B-Instruct** or **Qwen3-4B** — fine for constrained metric labelling with an *enumerated* candidate list + JSON grammar (Ollama `format`, GBNF, or outlines). Expect ~85–90% on prose lines, lower in dense tables.
- **Gemma 3 4B** — comparable; slightly better short-instruction adherence, slightly chattier.
- **Gemma 2 2B** — borderline. Use it only for the y/n fact filter, not multi-field extraction.

But: the README currently specs `qwen2.5:7b`, and your corpus is ~600 pages that ingests **once**. Going 3B doubles throughput to save an hour on a one-time run. Only worth it if you're CPU-only and 7B is genuinely overnight. If you have any GPU, keep the 7B — a Q4_K_M 7B beats a Q8 3B on this task at similar footprint.

## How to actually know

Hand-label ~60 lines from the three Delhivery PDFs (metric, period, basis, keep/drop). Run each candidate model against it, print accuracy per field. The corpus is small and fixed — one gold set answers this definitively instead of guessing. That's a ~40-line script, not a framework.

---

## Turn 3 — 2026-09-07 16:55

**Me:**

> remove regex as facts without numbers are in scope. Instead of using LLMs are there other ML models which can be leveraged for the same?

**Claude:**

Before the menu — one push-back, then the options.

## Don't remove regex; split the paths

The value-safety argument still holds for numbers: a transposed digit from a model is a wrong fact that *looks* right. The README's own trade-off table says numbers are out of scope "for **this path**" — implying a second path was always the plan. So:

- **Numeric facts** → deterministic extraction stays (upgrade it, see below)
- **Non-numeric facts** → new text path, where ML earns its place

Dropping regex entirely means reintroducing digit-corruption risk to save nothing.

## Non-LLM options, by sub-task

### Noise discrimination (replaces `_reject_reason`)
| Approach | What it is | Verdict for this project |
|---|---|---|
| **Claim-detection classifier** | Fine-tune DeBERTa-v3-small / MiniLM on ~300 labelled sentences: "checkable factual claim" vs boilerplate/heading/disclaimer | **Best fit.** Well-defined binary task, fast, runs on CPU. This is what ClaimBuster / CheckThat! do. |
| Sentence-type heuristics (spaCy) | POS/dependency rules — has finite verb, has subject, not a fragment | Cheap first filter, keep as a pre-screen |

### Fact structuring — (subject, predicate, object, qualifiers)
| Approach | What it is | Verdict |
|---|---|---|
| **REBEL** | 400M BART seq2seq, purpose-built to emit relation triplets | Least-bad non-LLM option. But trained on Wikipedia relations — weak on "appointed as CFO", "acquired for ₹X". Needs a relation allowlist + dedup. |
| **OpenIE6** / Stanford OpenIE | Extracts (arg1, relation, arg2) from any sentence, no schema | Famously noisy — near-duplicate triples, splits/merges wrong on complex sentences. Heavy post-filtering. |
| **Semantic Role Labeling** (AllenNLP) | Predicate + argument spans, PropBank style | Good for *event* facts (appoint/resign/acquire). ARGM-TMP / ARGM-LOC give you period/scope qualifiers nearly free. Combine with NER. |
| **spaCy NER + dependency rules** | `en_core_web_trf` for PERSON/ORG/DATE/MONEY + hand-written SVO / appositive patterns | Deterministic, fast, but *you* write and maintain the rules — the regex problem moved up a layer |
| FinBERT-NER / financial NER | Domain entities only | Identifies the pieces, not the relation — a component, not a solution |

### Numeric extraction (the current regex)
| Approach | Verdict |
|---|---|
| **Table Transformer (TATR)** / pdfplumber / Camelot | Genuinely better than regex — recovers 2D structure, which **solves the column→period alignment problem** the LLM is bad at. Deterministic. Use for financial statements. |
| Keep regex | For inline prose numbers ("revenue rose to ₹8,142 crore") where there's no table |

## Honest recommendation

For a **fixed ~600-page corpus that ingests once**, the pure non-LLM structuring stack (REBEL/OpenIE + SRL + NER + rules) is *more* dependencies, *more* rule maintenance, and *lower* accuracy than one constrained local-LLM call — and its output still needs an LLM-grade cleanup pass. The economics that justify it (millions of docs, latency budgets) aren't present here.

Where non-LLM clearly wins and you should adopt it:
1. **Claim-detection classifier** for noise — replaces the regex reject rules with something that generalizes to prose
2. **Table Transformer / pdfplumber** for numeric facts — deterministic *and* fixes column alignment

Where I'd keep the local LLM: turning a claim-classified sentence into `(subject, metric/predicate, value, qualifiers)`. That step needs semantics, and every non-LLM alternative is noisier.

If the constraint is "no LLM anywhere, it's a hard requirement" — say so and I'll spec the REBEL + SRL + NER pipeline properly. It's buildable, just more moving parts for less accuracy.

---

## Turn 4 — 2026-09-07 16:57

**Me:**

> so can we ask the llm to extract non numerical evidence as well?

**Claude:**

Yes — and the "evidence is verbatim by construction" guarantee survives, if you keep the same trick the number path uses.

## The trick transfers

The number path never let the model produce evidence: regex found a span, the model only labelled it. Do the identical thing for text:

1. **Deterministic sentence segmentation** (spaCy / PySBD) over each page. Every sentence gets a stable id, char range, `pdf_page`, `printed_page`.
2. Feed the model **numbered sentences** (10–20 per call, batched).
3. Model returns per sentence: `{id, is_claim, subject, predicate, object, qualifiers}` — it **points at** a sentence id, it never types the quote back.
4. You fetch `evidence.quote` from your own store by id. Verbatim, same guarantee as before.

Segmentation replaces regex as the thing that supplies spans. The model's job stays "classify and label," not "generate."

## What genuinely changes

For a number, regex gave you ground-truth `value`. For a text fact, the normalized `(subject, predicate, object)` **is** model-generated language — a paraphrase of the sentence. That's unavoidable and it's acceptable here, because:

- `statement` stays verbatim (the cited sentence). The structured triple is a *derived index field*, not the evidence.
- Text-fact verdicts are model-based in the design anyway ("appointed" vs "resigned" needs semantics) — the adjudicator re-reads the two `statement`s, it doesn't trust the triples blindly.

So: evidence verbatim ✓, value model-free ✗ for text facts — but the value was never going to be deterministic for "the board appointed X."

## Cheap grounding guard

After extraction, substring-check: do the `subject` and `object` strings actually appear (fuzzily) in the cited sentence? If the model cites sentence 12 but its entities are from sentence 9, reject. ~10 lines, catches the main failure mode.

## Practical

- Fold the noise filter into the same call: `is_claim: false` for headings, TOC lines, disclaimers, cross-references. Drop obvious page furniture with a few rules first so you're not paying model tokens for "Annual Report 2024".
- ~600 pages ≈ 20k sentences, batched 15/call ≈ 1.5k calls on the local 7B. A few hours, one time. Fine.
- No separate fine-tuned classifier needed for a fixed corpus — that was worth it only if this ran repeatedly.

Net: one unified pass — regex/table mines numbers, sentence-segmentation feeds text, the model labels both against spans it doesn't author.

---

## Turn 5 — 2026-09-07 17:25

**Me:**

> Three things the model turned out to be bad at
>
> Worth knowing, because each is now handled deterministically instead:
>
> It can't be trusted with periods. Asked to read a column headed March 31, 2023, qwen3:4b returned Q4-FY2024. That mapping is a pure function, so period_from() decides and the model only fills gaps where there's no header.
>
> Field order in the JSON schema is generation order. With is_fact first, it returned false for everything — including rows it then labelled correctly. Moving it last, so the model commits to subject and metric before ruling on facthood, fixed it outright. That's a general lesson for constrained decoding with small models.
>
> It still invents basis occasionally — one row came back provisional with nothing in the evidence saying so, despite an explicit "never infer it". Unfixed; good FAILURES.md material...are these fixable

**Claude:**

## #3 (invented `basis`) — fixable, with the pattern you already used twice

"Never infer it" is a negative instruction, and small models barely honor those. Don't instruct it — make inference structurally impossible, the same way you did for periods:

1. **Pre-fill from the section header.** Basis is almost always stated in a statement title or section heading ("Consolidated Financial Statements") and carried down the page — exactly the label-carry-forward `extract.py` already does for row labels. Scan the nearest preceding `consolidated|standalone` heading, fill it deterministically. Model never touches it when a header exists.
2. **Span-grounding for the gaps.** Where there's no header, require `basis_evidence: "<exact substring>"` alongside `basis`. Then verify deterministically: the substring must appear in the source line *and* contain a basis keyword. Fails verification → `basis = null`. The model can invent "provisional" all it likes; the verifier drops it.
3. **Default is `null`, never a guess.** A gap is missing data, not an inference opportunity.

That's ~15 lines and it's the identical move: deterministic where the document is explicit, verified-extractive otherwise, null by default.

**Why it's worth fixing, not just documenting:** `basis` is in the qualifier bag that drives the verdict. An invented "provisional" turns *values differ, all qualifiers identical* (CONTRADICTS, assignment case 2) into *differs on one qualifier* (RECONCILABLE, case 3). A hallucinated basis directly manufactures the wrong headline finding — it's not cosmetic.

**Still good FAILURES.md material** — keep it even after fixing. "Explicit instruction didn't work; span-grounding did" is a stronger entry than either a silent fix or an open bug, and it's the same lesson as the other two.

## #1 (periods) — fix is right, one calibration knob

`period_from()` as a pure function is correct. The one place it bites: FY naming isn't universal. `March 31, 2023` → FY23 for an Indian company (named by ending year), but the Economic Survey, RBI, and IMF excerpts may use calendar year or a different convention. Make the convention a per-source parameter, not a hardcoded rule — and log every case where the model fills a period with no header so you can audit that bounded set.

## #2 (field order) — fix is right and it generalizes

Generation order = commitment order for autoregressive models; `is_fact` first means everything after is conditioned on a decision made with no evidence in context. `is_fact` last is the correct lazy fix. You could push further — a one-phrase `reason` field just before `is_fact` — but you don't need to if last-position already fixed it on your set. Worth writing up as its own note: **for constrained decoding with small models, every judgment field goes after the fields it depends on.**

The through-line for all three: the model labels spans, it's not a source of facts. Every field needs either a deterministic derivation or a grounding check — periods got the function, numbers got the regex, basis needs the verifier.

---

## Turn 6 — 2026-09-07 17:34

**Me:**

> give proompt for fixing the same

**Claude:**

Assuming the model labels a regex-mined number (the numeric path). System prompt + input template + schema whose key order *is* the fix.

---

### System

```
You label a single number that has already been extracted from a financial or
economic document. You do NOT read the number, change it, or find new numbers.
Your job is to say what that number measures and under what qualifiers, using
ONLY the text provided.

Hard rules:
1. Every "*_evidence" field must be copied verbatim from PROVIDED TEXT. If you
   cannot copy a phrase that states it, the field is null. Do not reason it out,
   do not use outside knowledge, do not guess from what is "usual".
2. period, basis, vintage, scope are INDEPENDENT. A missing one is null, never
   filled by inference from the others.
3. Output the JSON keys in the exact order given by the schema. Decide subject
   and metric first; decide is_fact last.
4. is_fact = false when the number is a page/note reference, a date or year, a
   registration/identifier number, a table-of-contents entry, or the line states
   no measurable fact.
```

### Input template

```
SUBJECT (given):        delhivery-limited
SECTION HEADER (given): {section_header or "none"}
KNOWN BASIS (given):    {basis or "unknown — you may determine it from text below"}
KNOWN PERIOD (given):   {period or "unknown — you may determine it from text below"}

CANDIDATE NUMBER:       {value} {currency} {unit_raw}
ROW LABEL (carried):    {label}
SOURCE LINE:            {line}
CONTEXT (3 lines up):
{context_lines}

PROVIDED TEXT = SECTION HEADER + ROW LABEL + SOURCE LINE + CONTEXT. Quote only from these.
```

### Schema — key order is load-bearing

```json
{
  "metric":          "snake_case, e.g. revenue_from_operations",
  "metric_evidence": "verbatim phrase naming it, else null",
  "unit":            "INR_crore | INR_lakh | INR_million | percent | ratio | count | null",
  "period":          "fill ONLY if KNOWN PERIOD was unknown; else echo it",
  "period_evidence": "verbatim date/period phrase, else null",
  "basis":           "consolidated | standalone | null",
  "basis_evidence":  "verbatim phrase containing 'consolidated' or 'standalone', else null",
  "vintage":         "audited | provisional | restated | null",
  "vintage_evidence":"verbatim phrase, else null",
  "scope":           "short phrase e.g. 'excluding exceptional items', else null",
  "scope_evidence":  "verbatim phrase, else null",
  "is_fact":         "boolean — decided AFTER everything above",
  "confidence":      "0.0-1.0 — decided last"
}
```

### Few-shot (include 3–4)

```
PROVIDED TEXT:
  Consolidated Statement of Profit and Loss
  Revenue from operations
  Revenue from operations 8,142 7,225
  (₹ in crore) Year ended March 31, 2024 Year ended March 31, 2023
KNOWN BASIS: consolidated   KNOWN PERIOD: unknown
CANDIDATE: 8,142

{"metric":"revenue_from_operations","metric_evidence":"Revenue from operations",
 "unit":"INR_crore","period":"year ended March 31, 2024",
 "period_evidence":"Year ended March 31, 2024","basis":"consolidated",
 "basis_evidence":"Consolidated Statement of Profit and Loss","vintage":null,
 "vintage_evidence":null,"scope":null,"scope_evidence":null,
 "is_fact":true,"confidence":0.95}
```

```
PROVIDED TEXT:
  Notes to the financial statements (contd.)
  refer note 34 for related party transactions
KNOWN BASIS: unknown   KNOWN PERIOD: unknown
CANDIDATE: 34

{"metric":null,"metric_evidence":null,"unit":null,"period":null,
 "period_evidence":null,"basis":null,"basis_evidence":null,"vintage":null,
 "vintage_evidence":null,"scope":null,"scope_evidence":null,
 "is_fact":false,"confidence":0.98}
```

```
PROVIDED TEXT:
  Total income 9,001
KNOWN BASIS: unknown   KNOWN PERIOD: unknown
CANDIDATE: 9,001    (tests the basis-hallucination case)

{"metric":"total_income","metric_evidence":"Total income","unit":null,
 "period":null,"period_evidence":null,"basis":null,"basis_evidence":null,
 "vintage":null,"vintage_evidence":null,"scope":null,"scope_evidence":null,
 "is_fact":true,"confidence":0.6}
```

### Post-validation (not optional — this is what actually kills the hallucination)

```python
for f in ("metric", "period", "basis", "vintage", "scope"):
    ev = out.get(f + "_evidence")
    if ev is None or _norm(ev) not in _norm(provided_text):
        out[f] = None            # model couldn't ground it -> drop it
        out[f + "_evidence"] = None
if out["basis"] and out["basis"] not in ("consolidated", "standalone"):
    out["basis"] = None
```

---

- Run at `temperature 0`, `format: json` (Ollama) or a GBNF grammar pinning the enums and key order.
- `is_fact` last + `confidence` last is the #2 fix baked into the schema; the substring check is the #3 fix — the prompt asks nicely, the validator enforces.
- Skipped a grammar file; add one only if `format: json` lets key order drift on your model.

---

## Turn 7 — 2026-09-08 02:22

**Me:**

> now this ingestion of pdfs would take a very long time, what are the other alternative solutions which can be used to extract evidence from the said pdfs?

*[Claude ran 3 tools: Read ×2, Glob ×1]*

**Claude:**

Quick reframe from the current code: evidence extraction ([extract.py](extract.py)) is already model-free and runs in seconds — PyMuPDF + regex + band reconstruction. The cost is entirely [label.py](label.py): one `qwen3:4b` call per mined row, sequential, ~1.2k-token system prompt re-sent every time. On a ~600-page corpus that's ~10k+ calls. So the real question is *label fewer rows* and *label them faster*.

## Tier 1 — label less (free, 10–100×)

**Only label what can appear in a finding.** The assignment needs 4 cases across a handful of `subject|metric` groups. You don't need `total_income` from page 47 of the RBI report.

- Drop `accepted == False` (you already compute `reject_reason`).
- Keyword-prefilter `row_label` against a small target-metric list (revenue, PAT/net profit, GDP growth, CPI/inflation, total assets…). Fuzzy match, ~20 terms. 10k rows → a few hundred.
- Text excerpts: only label those mentioning a target subject or event verb (appointed/resigned/acquired/guidance). Most prose bands are boilerplate.

**Skip the model on clean table rows entirely.** For a well-formed row you already have `row_label`, `col_header`, `value`. `period_from(col_header)` is deterministic. `metric` is `row_label` → nearest canonical (embedding lookup, ~50× cheaper than a 4B generation). `basis_from(section)` is deterministic. The model's only marginal contribution on those rows is `is_fact`, which the regex accept/reject already approximates. Route the LLM to **text excerpts + rows where the metric lookup is low-confidence** only.

Tier 1 alone typically turns hours into minutes.

## Tier 2 — label faster (mechanical)

- **Batch.** Send 10–20 candidates per `/api/generate`, schema becomes an array. The ~1.2k-token preamble amortizes across the batch instead of per row. Biggest single speedup after Tier 1.
- **Concurrency.** `OLLAMA_NUM_PARALLEL=4` (more on GPU) + a `ThreadPoolExecutor` over rows. `label_one` is already independent per row.
- **Trim the prompt.** `format: SCHEMA` does the structural work; a 4B with a strict grammar rarely needs 3 few-shots. Cut to 1, drop `NUM_CTX` 4096 → 1024. Roughly halves prompt-eval.
- Keep the model resident (`keep_alive: -1`) so KV prefix cache survives between calls.

## Tier 3 — if still slow

- **Two-tier models:** `qwen3:1.7b` or `qwen2.5:3b-instruct` does `is_fact` triage on everything; only survivors hit the 4B for qualifiers.
- **Embeddings, not generation, for aliasing** (`nomic-embed-text`) — already in the README plan; adopt it now, it removes the hardest part of the labelling task from the LLM.

## Tier 4 — it's a one-time ingest

The corpus is fixed and you ingest it once. Renting one cloud GPU-hour, or a hosted Qwen endpoint (Groq/Together, ~$1–2 for the whole run), does the labelling pass in minutes. Keep the local Ollama path as the default in the repo; document the hosted path as "fast one-time ingest." The README's "no API spend" argument is about *ongoing* cost — a single bootstrap run doesn't break it.

## Recommendation

Do **Tier 1 + batching**. That's the 90% and it's maybe a half-day of work: a metric prefilter in `mine_document`, a deterministic-label fast path in `label_one`, and an array schema. Everything below that is only worth it if you measure and it's still too slow.

---
