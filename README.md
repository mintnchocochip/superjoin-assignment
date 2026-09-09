# Fact Knowledge Layer

Upload PDFs. Get facts, each tied to a verbatim quote on a real page, and the
relationships between facts from different documents: what corroborates what,
what contradicts what, and what only *looks* contradictory until you notice the
period, the scope, or the unit.

Four Python files and one HTML page. No database server, no queue, no graph
engine, and — by default — no API key.

### Why this is the second design

I built this once before, differently. That version is still in this repository's
history, on `evidence-mining-and-labelling`: a three-node MongoDB replica set with
TLS and RBAC, a local model served by Ollama, a deterministic regex miner writing
evidence separately from model-written claims, and a step where a human confirmed
which entity each document was about before anything was labelled.

It worked. The problem was where the engineering had gone — roughly 2,800 lines,
most of it infrastructure a reviewer must install (Docker, Ollama, a replica set,
a model download) before seeing a single grounded fact. That cost me more than it
would cost anyone else: every change to the part that actually matters, how facts
are discovered and compared, meant waiting on a stack that had nothing to do with
the question. The brief asks for a prototype whose behaviour is clear, and I had
built the opposite of that.

So I rebuilt around the shortest path from a PDF to inspectable output: 610 lines,
four dependencies, one command, no services. What follows is that system.

Two ideas from the first attempt are worth keeping and are honestly *not* in this
one — content-derived evidence ids, which make re-ingesting a document idempotent
rather than duplicating it, and sha256 deduplication on upload, which stops a
re-uploaded file corroborating itself. Both are in *Next Steps* for that reason.

```
llm.py        ask a model for JSON (Claude Code CLI, or a free API)
pipeline.py   PDF -> facts (grounded in quotes) -> relations (judged in pairs)
db.py         SQLite: documents, facts, relations
app.py        FastAPI: upload, read back, serve the page
static/       the UI
```

## Setup and Run Instructions

**One command. Python 3.10+ and nothing else.**

```bash
python run.py
```

That installs the four dependencies, starts the server, and opens
<http://127.0.0.1:8000>. There is no database to install, no container to start,
no model to download, and no API key to obtain — it uses the `claude` CLI already
on your machine. 
```bash
cd A:/superjoin-simple && GROQ_API_KEY=your_key python -m uvicorn app:app --port 8000
```

Drop PDFs on the page and watch the Documents tab: the status counts pages as
they are read (`extracting 7/18`), then links them. Facts appear when a document
finishes; **links appear once a second document has been processed**, since a
relation needs two documents to exist between.

A first look without processing anything: upload the three PDFs in
`starter-datasets/delhivery/` and open the **Links** tab.

### The model

**By default it uses the Claude Code CLI already installed on your machine** —
`llm.py` shells out to `claude -p` and parses the JSON that comes back. That
means no API key, no billing setup, and nothing to keep out of the repository.
If `claude` is on your PATH, the project runs.

It is not the only option. Set an environment variable and the same code path
uses a free hosted API instead:

| backend | how to select it | default model |
|---|---|---|
| `claude` *(default)* | nothing — needs the `claude` CLI on PATH | `claude-haiku-4-5-20251001` |
| `groq` | `GROQ_API_KEY=…` | `llama-3.3-70b-versatile` |
| `openrouter` | `OPENROUTER_API_KEY=…` | `meta-llama/llama-3.3-70b-instruct:free` |
| `gemini` | `GEMINI_API_KEY=…` | `gemini-2.0-flash` |

Override the pick with `LLM_BACKEND=groq`, the model with `LLM_MODEL=…`. Nothing
else changes: every backend answers the same two prompts and returns the same
JSON. The page header tells you which one is live.

**The CLI is the slow option.** Each `claude -p` call spends 20–60 seconds on
process startup, so a 25-page document takes a few minutes even with eight
running at once. A hosted free API answers in a second or two and is what you
want if you are processing a lot; the CLI is what you want if you would rather
not create an account. Both produce the same output.

### Knobs

| variable | default | what it does |
|---|---|---|
| `MAX_PAGES` | `20` | pages read per document; `0` means all. The upload form overrides it per file. |
| `WORKERS` | `8` | model calls in flight at once |
| `MAX_PAIRS` | `80` | pairs judged per new document |
| `FACTS_DB` | `facts.db` | SQLite file |

`python test_pipeline.py` runs the self-check on the two pieces of logic that
are not the model's job: quote grounding and candidate pairing.

## Video Demo

*(to record: upload a PDF, watch facts appear, then walk through the four cases
in the Links tab — under three minutes.)*

## Approach

### What counts as a fact

The document decides. There is no fact schema to conform to and no list of
metrics to look for — the extraction prompt asks for whatever the page states,
in the page's own vocabulary, and the row it fills in is deliberately loose:

```
subject | attribute | value | unit | period | scope | quote | page
```

`period` and `scope` are the two fields that earn their place. Almost every
apparent contradiction between real documents dissolves into one of them: FY23
against FY24, a quarter against a full year, adjusted against unadjusted, pro
forma against reported. Asking for them at extraction time is what lets the
second pass explain a disagreement instead of just flagging it.

`attribute` is free text, so a new kind of document brings new kinds of facts
without a migration — "receivable days" and "average tenure of directors" are
the same shape of row, and neither was anticipated.

### Grounding: the quote has to survive a check

The model must return a `quote` copied verbatim from the page. The quote is then
looked for **in the page text we sent it**, in three tiers — because a quote can
fail to match for three different reasons and only one of them means the model
made something up:

1. **It is there.** Whitespace and case may differ, and if that fails the
   comparison is retried on alphanumerics alone, because PDF extraction inserts
   stray spaces and breaks glyphs.
2. **Every word of it is there, in a different order.** This is what a slide deck
   looks like: `₹127Cr` sits in one text box and `EBITDA` in another, and the
   model reads them together the way a human would. Accepted, and marked
   *wording assembled from separate blocks on this page* so the UI can say so.
3. **Neither.** A value that is not on the page is a value the model wrote. It is
   stored, marked with the reason, shown in its own **Dropped** tab, and never
   linked to anything.

Getting tier 2 wrong was the single biggest quality bug in this project. Before
it existed, the earnings deck lost 72% of its facts to a grounding check that was
technically correct and practically useless. See *Case 4* below.

This is the cheapest honesty mechanism in the system. It turns "the model said
so" into "here is the sentence, on page 7, go look".

### Linking: cheap blocking, expensive judgement

Comparing every fact against every other fact is quadratic and would mean
thousands of model calls. Instead:

1. **Block cheaply.** Tokenise `subject + attribute`, drop stopwords, and score
   pairs by Jaccard overlap. Only pairs above 0.34 survive, at most four per
   fact, at most `MAX_PAIRS` per document. No embeddings, no dependencies.
2. **Judge expensively.** Each surviving pair goes to the model with both values,
   both periods, both scopes and both quotes, and comes back as
   `corroborates` / `contradicts` / `reconcilable` / `unrelated`, with a
   confidence and a written reason. `unrelated` is dropped.

The split is the whole design: the cheap half decides *what is worth comparing*,
the expensive half decides *what the comparison means*. Blocking is a recall
filter and is allowed to be dumb; judgement is where the reasoning lives and gets
the full context.

### Incremental by construction

A new document is extracted, then compared only against facts already in the
database. Nothing is re-extracted, no relation is recomputed, and the knowledge
layer grows one document at a time. Adding a tenth PDF costs the same as adding
the second, plus a larger candidate pool to block against.

### Trade-offs taken deliberately

- **SQLite over a graph database.** The relations table *is* the graph, and three
  joins answer every question the UI asks. A graph database would add an install
  step and answer nothing new — the assignment says as much.
- **Page-at-a-time extraction.** A fact that spans two pages is missed. In
  exchange, grounding is exact (the quote is checked against the page it came
  from), pages run in parallel, and a 400-page PDF is the same problem as a
  4-page one.
- **A cap on pages, not a summary.** Reading the first N pages of a long document
  is a blunt instrument, but it is honest and predictable. The alternative —
  asking a model to pick the interesting pages — hides what was skipped.
- **One document processed at a time**, with parallelism inside it. Two documents
  at once would just contend for the same model.
- **Facts are never deduplicated.** Two documents stating the same number produce
  two facts and one `corroborates` edge between them. Merging them into one
  canonical fact would throw away the thing the assignment asks for: which
  document said it, and where.

### AI tools used

Written with Claude Code (Opus 5). The extraction and judgement prompts were
tuned against real pages from the starter dataset — mostly by reading what came
back and tightening the rules that were being ignored. The `encoding="utf-8"`
argument in `llm.py` is there because a first run stored `â‚¹` where the source
said `₹`; Windows was decoding the CLI's UTF-8 output as cp1252.

## The Four Required Cases

All four come from the Delhivery documents, processed through the UI with nothing
configured but the page cap. Every quote below is what the system stored and has
been checked back against the source PDF; every *Reasoning* line is what the model
wrote when it was shown that pair. Neither is edited here.

One caveat worth stating plainly: the judge is a language model, so a pair's
verdict is not bit-identical between runs, and which pairs get judged at all
depends on the `MAX_PAIRS` cap. Cases 1 and 3 reproduce on every run of these two
documents. Case 2 reproduces at the judge — shown that pair, the model returns
`contradicts` at 0.92 — but the pair only reaches the judge when the cap is not
already full, which is exactly the bug described under that case.

### 1. A fact corroborated across documents, expressed differently

The same FY24 figure, in two documents, in two different units — one in millions,
one in crore — with neither document using the other's wording.

| | |
|---|---|
| **A** | `02-delhivery-annual-report-fy24-excerpt.pdf`, p.4 |
| | Revenue from services = **81,415 INR million**, period FY24 |
| | *"Revenue from services ₹81,415Mn"* |
| **B** | `03-delhivery-q4-fy24-earnings-presentation.pdf`, p.6 |
| | Revenue from services = **8,142 Cr**, period FY24 |
| | *"₹8,142 Cr FY24 revenue from services"* |

**corroborates** · confidence 0.98

> Both facts report FY24 revenue from services for the same entity. Fact A states
> ₹81,415 million; Fact B states ₹8,142 Cr. Converting units: 8,142 Cr = 81,420
> million, which differs from Fact A by only 5 million (0.006% variance) — a
> negligible rounding difference that confirms agreement.

The unit conversion and the 5-million rounding gap are the model's work. The
blocking step never looked at the numbers at all — it paired these two because
`revenue`, `from` and `services` overlap.

### 2. A genuine or likely contradiction

Two figures for Adjusted EBITDA that cannot both be true under one definition:
a single quarter reported larger than the full year that contains it.

| | |
|---|---|
| **A** | `02-delhivery-annual-report-fy24-excerpt.pdf`, p.4 |
| | Adjusted EBITDA = **758 INR Mn**, period FY24 |
| | *"₹758Mn Adjusted EBITDA"* — wording assembled from separate blocks |
| **B** | `03-delhivery-q4-fy24-earnings-presentation.pdf`, p.7 |
| | Adjusted EBITDA = **92 ₹ Cr**, period Q3 FY24 |
| | *"Q3 FY24: ₹92 Cr / 4.2%"* |

**contradicts** · confidence 0.92

> Both report Delhivery's Adjusted EBITDA, but Q3 FY24 of ₹92 Cr (920 Million)
> exceeds the stated full-year FY24 figure of ₹758 Mn — a single quarter cannot
> exceed the entire fiscal year. The values are mathematically incompatible unless
> one extraction misidentified the metric (e.g. the 92 Cr refers to revenue rather
> than EBITDA, which the '/4.2%' in the evidence might suggest).

Not a typo: two disclosures that only conflict once you put them in the same unit
and notice one period sits inside the other. The model's own last clause is the
useful part — it names the likeliest innocent explanation and points at the
evidence that suggests it. The system's job here is to hand a reviewer the two
pages, not to settle it.

**This case also exposed a real bug, and is the reason for a change in
`candidates()`.** Both facts are extracted on every run, and the pair scores 0.50
on blocking — comfortably above the 0.34 threshold. But in a run with more
documents it produced *no* edge, because `MAX_PAIRS` cut the candidate list at
exactly 80 and this pair fell below the line. The judge had never seen it.

Two things came out of that. Pairs are now ranked by their actual overlap score
before the cut, rather than by a proxy that quietly favoured long attribute names.
And the number of pairs the cap discarded is now reported on the document row,
because **a pair the model never saw is not the same as a pair it found
unrelated**, and only one of those is a statement about the documents.

**Contradictions are rare here, and that is a finding rather than a gap.** These
documents return dozens of reconcilable pairs and a handful of corroborations for
every contradiction, because a company's own filings mostly agree with themselves
and disagree only across periods. A result set full of contradictions would mean
the judge was too eager, not that the documents were interesting.

### 3. An apparent contradiction explained by context

The *same metric*, in the *same two documents* as Case 1, given a different
verdict — because the periods do not line up.

| | |
|---|---|
| **A** | `02-delhivery-annual-report-fy24-excerpt.pdf`, p.4 |
| | Revenue from services = **81,415 INR million**, period **FY24** |
| | *"Revenue from services ₹81,415Mn"* |
| **B** | `03-delhivery-q4-fy24-earnings-presentation.pdf`, p.7 |
| | revenue from services = **2,076 Cr**, period **Q4 FY24** |
| | *"₹2,076 Cr Q4 FY24 revenue from services"* |

**reconcilable** · confidence 0.95

> FACT B (₹2,076 Cr Q4 FY24 = ₹20,760 Mn) is a quarterly subset of FACT A
> (₹81,415 Mn full FY24). The Q4 value represents ~25.5% of annual revenue, a
> reasonable proportion for one quarter. The difference in period (Q4 vs full
> year) fully explains the apparent difference in values.

Cases 1 and 3 are the argument for `period` and `scope` being extracted as fields
in their own right. The same metric name, the same pair of documents, and a
4× difference in value — and the only thing separating "these agree" from "these
are not comparable" is a period string the extraction step was asked for
explicitly. Without it, both pairs look like contradictions.

The other reconciliations found on this corpus follow the same pattern in
different clothes: a full year against a quarter, FY23 against FY24, an adjusted
margin against an unadjusted one, and a figure "since inception" against one for
a single year.

### 4. An extraction failure I found, and what I did about it

Three, in the order I hit them. Each was found by reading the output, not by a
test failing.

**A quote that is real but proves nothing.** The system paired two facts for
`Daily average fleet size = 15,065` and called it corroboration at 0.95. The
evidence on one side was the whole quote:

> *"Daily average fleet size"*

A real line of the annual report, containing no number. The figure sits elsewhere
on the page, and the grounding check — which only asked "is this text on the
page?" — was satisfied. **Fixed:** a fact whose value contains digits must have
those digits inside its own quote, or it is dropped with that reason recorded.

**A table row without its column headers.** In the earnings deck, quarterly
tables extract as a flat run of numbers:

> *"Fleet size – daily average    9,120    11,105    13,688    15,065"*

The model has to guess which column belongs to which quarter, and it does not
always guess right — the same row produced one correct pairing and one where a
value was attributed to the wrong quarter. Grounding cannot catch this, because
every number in the row genuinely is on the page. **Not fixed.** It is the clearest
argument for parsing tables as tables (PyMuPDF's `find_tables()`) and carrying the
column header into `period`, instead of handing the model a flattened row.

**A silent failure that looked like an empty document.** One run finished with a
document marked `done` and zero facts. The model calls had failed, `ask_json`
swallowed the errors by design so one bad page could not kill a document, and the
result was indistinguishable from a PDF that simply said nothing. **Fixed:** failed
calls are counted and the first reason is written to the document row, where the
UI already shows it. A document that got nothing now says why it got nothing.

The first fix cost the system some true facts — the fleet-size figure is real,
and it is now dropped rather than kept on bad evidence. That trade is deliberate:
a fact layer whose evidence does not support its facts is worse than a smaller
one that can be checked.

## Limitations and Next Steps

**Blocking is lexical.** "Topline" and "revenue from operations" never get
compared, because they share no words. This is the biggest recall hole in the
system. An embedding index over `subject + attribute` would close most of it
without changing anything downstream.

**The `MAX_PAIRS` cap decides what never gets compared.** Ranking by overlap and
reporting how many pairs were discarded makes the cost visible, but it does not
remove it: on a large corpus the 81st-best candidate is simply not examined, and
Case 2 is a live example of a real contradiction falling off that list. Raising
the cap costs model calls linearly. The principled fix is to spend the budget
where it is informative — judge pairs whose *values* disagree first, since two
facts that already agree are the cheap case to confirm and the least interesting
to be told about.

**Judgement is pairwise, so there is no consensus.** If three documents state a
figure and one disagrees, you get three independent edges, not "two against one".
Clustering the corroborating edges and reporting the majority would be a better
answer, and the data model already supports it.

**Scanned PDFs produce nothing.** There is no OCR; a document with no text layer
fails with a message saying so.

**Cross-page and table facts.** A number in a table whose header sits on the
previous page is either missed or mis-attributed.

**Confidence is the model's self-report.** It is useful for ordering the list and
should not be read as calibrated.

**Two things the first design did better.** Evidence ids were derived from the
evidence content, so re-ingesting a document reconciled instead of duplicating;
here a second upload of the same file creates a second set of facts. And uploads
were deduplicated by sha256, which matters more than it sounds — without it, a
file uploaded twice will cheerfully corroborate itself, and the knowledge layer
will show you agreement that is really just a copy.

**Next, in order:** sha256 dedup on upload (smallest fix, real correctness bug);
embeddings for blocking; consensus across three or more documents instead of
pairwise edges; a highlight of the quote inside the rendered PDF page rather than
a link to it; and re-judging existing pairs when a later document changes the
picture.

## Additional Notes

- `uploads/` and `facts.db` are gitignored. Delete `facts.db` to start clean.
- Every number in the UI is one click from the page it came from — the document
  name in any fact opens the PDF at that page.
- The `Dropped` tab is not an error log to be emptied. It is a permanent part of
  the interface, because knowing what the system refused to believe is as useful
  as knowing what it accepted.
