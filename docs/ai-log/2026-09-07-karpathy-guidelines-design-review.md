# Karpathy guidelines design review

Session `3623c6ed` · 3 turns

## Turn 1 — 2026-09-07 13:25

**Me:**

> *(invoked `/andrej-karpathy-skills:karpathy-guidelines`)*

*[Claude ran 3 tools: Bash ×2, Read ×1]*

**Claude:**

Read the brief and both dataset READMEs. Here's the differential.

## Verdict

The skeleton is right — fact-centric store, evidence links, progressive compilation. But there's **one missing box that the entire assignment hinges on**, and one modelling mistake that will bite you the moment you write code.

---

## What holds up

**Fact-as-first-class-record with evidence links.** The brief's second bullet is literally "links every fact to evidence in its source document." You have it. Good.

**Progressive compilation as PDFs arrive.** That's brownie point #4 (incremental, no full rebuild) baked into the base design rather than bolted on. Cheap to claim, worth claiming.

**Numeric facts as a distinct concern.** Correct instinct — numeric facts are where contradiction is *mechanically* detectable rather than a vibes judgment. That's where your demo cases will come from.

---

## What breaks

### 1. There is no matching step. This is the missing box.

Your diagram goes `Numerical Data → Facts → Contradictions`. But **contradiction detection is a join, and you haven't drawn a join key.**

Two facts can only be compared if you first agree they're *about the same thing*. "Revenue from operations, FY24, consolidated, ₹ crore" and "Revenue, FY2024, ₹ mn" and "revenue of $X" are the same fact in three costumes. Nothing in your design turns them into the same costume.

You need a **canonicalization / normalization layer** between extraction and the fact store, producing a comparable key:

```
subject   : Delhivery Limited | India
metric    : revenue_from_operations | real_gdp_growth
period    : FY2024 | Q4-FY2024 | CY2024
unit      : INR_crore  (value normalized)
basis     : consolidated | standalone | restated | provisional
```

Everything downstream — corroboration, contradiction, reconciliation — is `group by canonical_key`, then compare values. Without this box you have a pile of facts you cannot compare, and no amount of LLM cleverness at query time fixes it, because the comparison set is unbounded.

### 2. `reconciliation / corroborations / contradictions` as children of `Facts` is a category error

A fact is never "a contradiction." A **pair** of facts is contradictory. You've drawn a relation as an attribute, and that will fight you in the schema.

Two collections, not one:

- `claims` — atomic, one per extracted fact, with evidence + canonical key
- `relations` — edges: `(claim_a, claim_b, type, reason, discriminating_dimension, confidence)`

where `type ∈ {corroborates, contradicts, reconcilable}` and `discriminating_dimension` is *the thing that explains the apparent conflict* — `period`, `basis`, `unit`, `vintage`. That field is your case #3 ("apparent contradiction explained by context") for free: an apparent contradiction is a value mismatch where exactly one qualifier differs.

### 3. Most "contradictions" you find will be false positives

This is the failure mode your current design walks directly into, and it's worth internalizing before you build.

Your macroeconomy set is Economic Survey + RBI + IMF on the same economy. They will disagree on GDP growth, CPI, CAD — not because anyone is wrong but because of **data vintage, revision cycle, fiscal-year convention, and provisional vs. revised estimates.** Same for Delhivery: annual report vs. Q4 presentation will show different revenue because one is FY and one is quarterly, and consolidated ≠ standalone.

If facts don't carry qualifiers, a naive value comparison flags all of these as contradictions and your demo drowns in noise. **The qualifier bag is not a nice-to-have; it's the thing that makes the output non-embarrassing.**

Corollary: a *genuine* contradiction (case #2) is rare and precious. Budget time to hunt for one — the director-active-vs-resigned shape, or a figure restated between the prospectus and the annual report, is a better bet than hoping one falls out.

### 4. Splitting `Numerical` from `Contextual` storage is premature and lossy

The qualifiers that let you reconcile a number **live in the surrounding prose and the table header.** Route numbers one way and text the other and you've severed the value from the thing that explains it.

Split the *comparator*, not the *storage*. One `claim` record; optional `value`/`unit` fields when numeric; a numeric comparator runs on rows that have them, a semantic comparator on the rest. This also fixes your schema-evolution brownie point — a hardcoded two-way split is anti-evolution, a `fact_type` discovered at ingest is not.

### 5. `NLP` as a box is a non-answer, and the arrow is bidirectional

Two problems.

The box hides your most important decision: does a query run *retrieval over structured facts*, or *RAG over document text*? The brief pointedly says a graph or visualization alone isn't the solution — "the interesting part is how facts are discovered, grounded, compared, and explained." So the query path should return **a fact set + its evidence + its relations**, with prose as a thin narration layer on top. Not prose with citations stapled on.

The bidirectional arrow implies queries write facts back. If extraction happens at query time, your "compiled progressively at ingest" claim is false and results become non-deterministic across runs. Make it read-only and say so.

### 6. Evidence granularity is under-specified, and this dataset has a trap

"Linked to pdf" won't survive the demo. You need `doc_id + page + verbatim quote span`.

The trap: **both curated READMEs warn that printed page numbers jump** because sections were excised. Printed page ≠ PDF page index. Store the PDF page index as the anchor and the printed label as a display string, or your citations will point at the wrong page in exactly the documents you're demoing.

### 7. `Info Extractor` as one pass over a 100-page financial PDF will not work

Six documents, 89–100 pages each, dense with financial tables. A single LLM pass blows context, costs real money, and hallucinates numbers. You need chunking, and specifically **table-aware** handling — financial figures live in tables, and naive text extraction collapses column alignment, which silently reassigns numbers to the wrong row label.

Silver lining: that is almost certainly your case #4 ("an extraction failure you found"). It's a genuinely interesting one — column misalignment producing a confidently-wrong fact with correct-looking evidence. Plan to find it rather than stumble into it.

### 8. Pairwise comparison is O(n²) and you've drawn no blocking step

"Compiled progressively as PDFs are added" means each new claim gets compared against the existing corpus. Six documents will yield thousands of claims. Naive pairwise LLM adjudication is thousands of calls per upload.

Fix is cheap: **block on the canonical key** (exact or near match) and only run the LLM adjudicator on the resulting candidate pairs. This is where the incremental brownie point is actually earned — new doc touches only the claim groups it collides with.

### 9. No confidence, no rationale on judgments

The brief asks for "sensible handling of ambiguity and uncertainty." Every relation edge needs a confidence and a one-line reason. That reason string *is* your explanation UI.

### 10. Minor: no API/UI layer in the diagram

The brief requires an upload endpoint and an inspection surface. `user input pdfs` isn't that. Trivial to add, but the checklist item is explicit.

---

## The corrected pipeline

```
upload → page-aware, table-aware chunking
       → per-chunk claim extraction (LLM), each with verbatim span + page index
       → NORMALIZE: canonical key (subject, metric, period, unit, basis) + value  ← the missing box
       → BLOCK: group by canonical key / near-neighbors                            ← makes it tractable
       → ADJUDICATE candidate pairs (LLM) → relation edge
             { type, reason, discriminating_dimension, confidence }
       → query: filter claims → return claims + evidence + edges → narrate
```

Two collections: `claims`, `relations`. That's the whole model.

---

## What to skip

Skip the graph database — the brief says so explicitly, and `relations` is a table with two foreign keys. Skip embeddings until exact-key blocking demonstrably misses; add them when it does. Skip the UI beyond an upload box and a fact table.

**One process note that will save you the most time:** pick your four demo cases from the actual PDFs *first*, then build backwards to make those four work. The brief grades on the four cases, not on corpus coverage. Building generally and hoping the cases fall out is the expensive path.

---

## Turn 2 — 2026-09-07 13:31

**Me:**

> I am not using graphdb, imma use mongodb collections. Where pdfs is a collection, numerical data (with context extracted) as another, textual information as another collection. Evicidene is a collection where serveal similar facts or opposing facts are are linked there to that evidence. they evidience are given a tag which says are they contradicting or agreeing together.

**Claude:**

Mongo is the right call — `relations` was always a table with two foreign keys, not a graph. Four issues with the shape you've described, one of which is just naming and one of which is genuinely load-bearing.

## 1. You've named the relation collection "evidence" — that word is already taken

The brief uses *evidence* to mean **the source snippet backing a single fact**: "links every fact to evidence in its source document." What you're describing is a **cluster of related claims** with a verdict on it. Different thing.

Two costs. A reviewer reading your README against the checklist ("Results contain facts, source evidence, and cross-document relationships") will look for evidence and find a relation table. And internally you now have no name left for the actual quote span, so it ends up as an unnamed blob on the fact.

Rename: `claims` + `claim_groups`. Evidence is an **embedded subdocument on each claim**, never its own collection — it's 1:1 with the claim and always fetched with it, so a separate collection is a join for nothing.

## 2. Collapse `numerical` and `textual` into one collection

I flagged this before; you kept it. Here's the Mongo-specific argument rather than the abstract one.

You split collections when access patterns or indexes differ. Here they're **identical** — both get looked up by "what topic is this about" during grouping, and both get returned by the query API. So the split buys zero and costs a union on every single downstream operation: blocking, adjudication, query, the UI table. You'll write `$unionWith` everywhere or query twice and merge in app code, forever.

Then the cases that don't fit:

- *"Revenue grew 12% to ₹X crore"* — numeric **and** semantic. Duplicate it, or lose half?
- A director's status, an address, a date, a policy stance — brownie point #3 is "a schema that evolves dynamically as new kinds of facts appear." Your split is a fixed two-way taxonomy chosen up front. Third fact type → third collection?

One `claims` collection. `value`/`unit` present when numeric, absent when not. If you want the separation for queries, that's a `fact_type` field plus a partial index — the Mongo-native version of what you're reaching for, at zero join cost.

## 3. Still nothing that decides *which* facts go in a group — and this is the load-bearing part

Your evidence collection presupposes something already determined that these facts belong together. That decision is the whole system and it's still not in the design.

Here's the version worth building, because **it mechanically generates three of your four required cases.**

Use a **two-level key**. Group loosely, discriminate tightly.

**Level 1 — grouping key (loose):** `(subject, metric)`. `(Delhivery, revenue_from_operations)`. `(India, real_gdp_growth)`. This is what puts claims in the same group.

**Level 2 — qualifiers (not part of the key):** `period`, `basis` (consolidated/standalone), `vintage` (provisional/revised/restated), `unit`, `scope`.

Now the verdict falls out of comparing two members of a group, after normalizing units:

| values | qualifiers | verdict | your case |
|---|---|---|---|
| match | any | **corroborates** | case 1 |
| differ | differ in exactly one dimension | **reconcilable** — name the dimension | case 3 |
| differ | identical | **contradicts** | case 2 |

That third row is the rare, precious one. The middle row is where nearly all of your macroeconomy pairs will land — Economic Survey vs RBI vs IMF differing on GDP growth by *vintage*, and the system says so by name instead of crying contradiction.

The reason this matters: **grouping key tightness decides which cases you can even demo.** Put `period` in the key and apparent-contradictions never group, so case 3 becomes undemonstrable. Leave it out of the key and put it in qualifiers, and case 3 generates itself.

## 4. One tag per group breaks at three members

"Given a tag which says are they contradicting or agreeing" — works for two claims. Group `(Delhivery, revenue_from_operations)` will hold five or six across three documents, where A and B agree, C is a different period, D contradicts A outright.

The verdict lives on the **pair**, not the group. Keep the group as the container, store an array of pairwise verdicts inside it.

## The schema

```js
// claims
{
  _id, doc_id,
  subject: "Delhivery Limited",
  metric:  "revenue_from_operations",
  value: 8141.7, unit: "INR_crore",        // absent for non-numeric
  qualifiers: { period: "FY2024", basis: "consolidated", vintage: "audited" },
  statement: "Revenue from operations for FY24 stood at ...",   // as written
  evidence: { pdf_page: 47, printed_page: "112", quote: "...", bbox: [...] },
  group_key: "delhivery-limited|revenue_from_operations",
  confidence: 0.9
}

// claim_groups
{
  _id: "delhivery-limited|revenue_from_operations",
  subject, metric,
  verdicts: [
    { a: ObjectId, b: ObjectId, type: "reconcilable",
      discriminating_dimension: "period",
      reason: "FY24 full-year vs Q4 FY24 standalone quarter",
      confidence: 0.85 }
  ]
}

// pdfs
{ _id, filename, sha256, page_count, uploaded_at, path }
```

`group_key` on the claim, not a `claim_ids` array on the group. Ingest becomes one insert plus a recompute of that group's verdicts — no `$push` contention on hot group docs when two uploads land together.

## Mongo practicalities

**Index `claims.group_key`.** That index *is* your blocking step — the thing that keeps ingestion off O(n²). New claim → one indexed lookup → adjudicate only against that group's members. Typically 2–6 comparisons, not thousands. Your incremental brownie point lives entirely in this one index.

**Store the PDF path, not the PDF.** Your files are 2–5MB so BSON's 16MB limit isn't hit, but a `pdfs` doc you query for metadata shouldn't drag a 4MB binary through every read. Filesystem path, or GridFS if you want it self-contained.

**`sha256` on `pdfs`, unique.** Re-uploading the same file otherwise doubles every claim and manufactures fake corroborations — a system that congratulates itself for finding agreement between a document and its own copy is a bad demo.

---

Net: drop `evidence` as a collection, merge numerical+textual into `claims`, add `group_key` + qualifiers, move the tag onto pairs. Three collections, one index, and your four cases come out of the table above rather than out of prompt engineering.

---

## Turn 3 — 2026-09-07 13:33

**Me:**

> give me a system architecture for the same

*[Claude ran 1 tool: Skill ×1]*

**Claude:**

No source files yet, so I'll assume **Python + FastAPI**. Say the word if you'd rather have TypeScript — the shape is identical, only the SDK calls change.

## Stack

| Layer | Choice | Why |
|---|---|---|
| API | FastAPI + `BackgroundTasks` | Ingest takes minutes; background tasks give you async without adding Celery/Redis |
| PDF | PyMuPDF (`fitz`) | Text + page index + bbox from one dependency |
| Store | MongoDB, 3 collections | Yours |
| LLM | Anthropic SDK, `claude-opus-5` | Structured outputs + `strict: true` tools |
| UI | One static HTML page against the API | Not React |

## Pipeline

```
  POST /documents ──▶ FastAPI ──▶ sha256 dedup ──▶ pdfs{path, pages, status}
                         │
                         │ BackgroundTasks
                         ▼
┌─────────────────────── INGEST ────────────────────────────────────┐
│                                                                    │
│  1. SPLIT       PyMuPDF → page text + pdf_page_index   DETERMINISTIC│
│  2. EXTRACT     1 call/page, strict tool schema        LLM (parallel)│
│  3. GROUND      quote must string-match page text      DETERMINISTIC│
│  4. NORMALIZE   units · periods · subject slug         DETERMINISTIC│
│  5. INSERT      claims{group_key, qualifiers, value}                │
│  6. BLOCK       index lookup on group_key              DETERMINISTIC│
│  7. ADJUDICATE  compare against group members          DET → LLM     │
│  8. UPSERT      claim_groups{verdicts[]}                            │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
                         │
  GET /groups /findings ◀┘
```

---

## Stage notes

**1 · Split — one page = one chunk.** No sliding windows, no semantic chunking. A page is a natural unit, fits context comfortably, and hands you the evidence anchor for free. Store `pdf_page_index` (0-based, real) *and* `printed_page` (the label on the page) — remember your excerpts have jumping page numbers.

**2 · Extract — one call per page, `strict: true` tool.** Force schema-valid output so you never parse malformed JSON. Ask for `subject, metric, value, unit, period, basis, statement, quote`. Empty array is a valid answer for a page of boilerplate.

Start with PyMuPDF text in a text prompt. **When the annual report's financial tables come out column-scrambled — and they will — switch those pages to native PDF `document` blocks** so the model sees the table visually instead of as mangled text. Staged, not both up front. That switch is also your case #4 write-up: "text extraction silently reassigned figures across columns; here's the wrong fact it produced, here's the fix."

**3 · Ground — the cheapest hallucination guard you'll ever write.**

```python
if claim.quote not in page_text:
    reject(claim)   # model invented or paraphrased it
```

Three lines. Kills fabricated evidence, and it's a strong line in your README: *every stored fact's quote is verified to exist verbatim in its source page.*

**4 · Normalize — Python, not the LLM.** The model emits raw strings; deterministic code canonicalizes them:

- units: `crore | lakh | million | bn | ₹ | INR | Rs.` → base `INR`
- periods: `FY24 | FY 2023-24 | 2023-24 | Q4FY24` → `FY2024` / `Q4-FY2024`
- subject: strip `Limited|Ltd|Inc`, lowercase, slug → `delhivery-limited`
- `group_key = f"{subject}|{metric}"`

Metric naming is the loose joint — let the model propose a snake_case metric string and keep a small alias map you grow when you see collisions. Don't build an ontology.

**6 · Block — this one index is your whole scalability story.**

```python
db.claims.create_index("group_key")
```

New claim → one indexed lookup → compare against 2–6 existing members. Not thousands. Brownie point #4 (incremental, no rebuild) lives entirely here.

**7 · Adjudicate — and here is the design's spine.**

## Deterministic vs LLM

Draw this line explicitly in your README; it's the strongest engineering decision in the system.

| Job | Who | Why |
|---|---|---|
| Extract claims from a page | **LLM** | Genuinely needs language understanding |
| Verify quote exists in source | Python | String match |
| Normalize units & periods | Python | Pure functions, testable |
| Group membership | Mongo index | Not a judgment call |
| **Numeric verdict** (agree/contradict/reconcilable) | **Python** | It's the comparison table — reproducible, auditable, free |
| Write the human-readable *reason* | **LLM** | Prose |
| Verdict on non-numeric claims | **LLM** | "Director active" vs "resigned" needs semantics |

The numeric verdict is the table from earlier, as code:

```python
if values_equal(a, b, tol):              "corroborates"
elif differing_qualifiers(a, b) == 1:    "reconcilable"   # name the dimension
else:                                    "contradicts"
```

Your headline capability — cross-document contradiction detection over numbers — is therefore **deterministic and reproducible**, not a prompt that might behave differently on Tuesday. The LLM only narrates. That's the thing a reviewer will actually notice.

## API surface

```
POST /documents            upload → {doc_id, status}
GET  /documents/{id}       {status, pages_done, claims_found, errors[]}
GET  /claims?subject=&metric=
GET  /groups/{group_key}   members + evidence + pairwise verdicts
GET  /findings?type=contradicts|reconcilable|corroborates
```

`/findings` is the demo endpoint. Your 3-minute video is: upload a PDF → watch status → hit `/findings?type=contradicts` → open a group → two quotes side by side with page numbers and a verdict. That's three of the four required cases on one screen.

## Models and what it costs

| Leg | Model | Config |
|---|---|---|
| Page extraction (~600 calls) | `claude-opus-5` | `output_config: {effort: "low"}`, `strict: true` tools |
| Verdict reasons (~500 short calls) | `claude-opus-5` | default effort |

Full-corpus ingest — all six PDFs, ~600 pages — runs roughly **$13** on Opus 5. Not enough money to justify downgrading. Two levers if you want them, your call:

- **`claude-haiku-4-5`** ($1/$5 vs $5/$25) on the extraction leg only → ~$3. Extraction is schema-constrained and grounded by the quote check, so it's the leg that tolerates a smaller model best.
- **Batch API** — 50% off, async, and ingestion is already a background job. Halves whichever you pick. This is also your honest answer to "many PDFs without performance issues."

Cache the system prompt + tool schema across the 600 extraction calls — same prefix every time, so it should be a near-100% cache hit rate. Check `usage.cache_read_input_tokens` is non-zero.

## Robustness, cheap

- `sha256` unique index on `pdfs` — a re-uploaded file otherwise manufactures fake corroborations between a document and its own copy.
- `status: pending|processing|done|failed` on `pdfs`; poll it from the UI.
- On extraction failure, store the raw model output on an `extraction_errors` collection. Free material for your Limitations section.
- `asyncio.Semaphore(8)` on the extraction fan-out. Don't fire 600 concurrent requests.

## Deliberately absent

No queue broker, no vector DB, no graph layer, no auth, no React, no Docker. Add embeddings only when exact `group_key` matching demonstrably misses a pair you care about — and if that happens, it's one more field plus Atlas Vector Search, not a new service.

---

Want this as a shareable page for your README's Approach section? One line and I'll publish it.

---
