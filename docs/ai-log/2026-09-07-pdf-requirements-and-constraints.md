# PDF requirements and constraints

Session `b34fc688` · 3 turns

## Turn 1 — 2026-09-07 11:15

**Me:**

> read the pdf and elucidate the requirements and other constraing in a easy to understand manner.

*[Claude ran 3 tools: Read ×2, Bash ×1]*

**Claude:**

I'll look for the PDF first.

Two PDFs. Reading both.

## The task, in one sentence

Build a system that reads PDFs, pulls out **facts**, keeps a **pointer back to where each fact came from**, and then figures out how facts from *different* documents relate to each other — agree, disagree, or only *look* like they disagree.

Note: `[redacted].pdf` is a bonafide certificate, unrelated. And the **three starter PDFs mentioned in the assignment are not in this folder** — you'll need those.

---

## Hard requirements (must have)

**1. Extract facts**
Numerical or semantic. "Revenue was ₹12.4 Cr in FY24", "Priya Sharma is a director", "registered office is at X". What counts as a fact is *derived from the documents*, not a schema you hardcode.

**2. Ground every fact in evidence**
Each fact must link back to its source — document, page, and ideally the exact sentence/span. No orphan facts.

**3. Relate facts across documents**
Three relation types:
- **Corroborates** — same claim, different wording ("₹12.4 Cr" vs "1.24 billion rupees")
- **Contradicts** — genuinely incompatible
- **Reconcilable** — looks contradictory but isn't, once you account for **time / scope / units** (Q3 vs full-year revenue; ₹ vs $; group vs subsidiary)

**4. An API or UI**
They must be able to upload a PDF and inspect the results themselves. Not a static report.

**5. Generalize**
They will test with *their own* PDFs. So: no hardcoded filenames, no hardcoded fact lists, no "if document is annual_report.pdf then...". This is the constraint most likely to sink a submission.

---

## Hard constraints (things that will fail you)

| Constraint | What it means |
|---|---|
| No hardcoding | facts, filenames, schemas, or document-specific rules — all forbidden |
| A graph DB is not the answer | Neo4j + a pretty visualization alone = fail. The *discovery, grounding, comparison, explanation* is the deliverable |
| No credentials in the repo | If you use a paid API, include sample outputs + video so they can evaluate without your key |
| Demo video ≤ 3 minutes | Hard cap |
| Meaningful git history | Not one "initial commit" dump |

---

## The four cases you must demonstrate

This is the actual grading rubric. Your submission needs **at least one worked example of each**:

1. **Corroboration across documents**, even when phrased differently
2. **A genuine (or likely) contradiction**
3. **An apparent contradiction resolved by context** — time, scope, or units
4. **A failure** — something your system got wrong, plus how you handled it or would fix it

For **1–3 you must show the source evidence AND your system's reasoning.** Not just a label — the *why*.

Case 4 is the honesty check. Don't skip it and don't fake a trivial one; they explicitly ask you to be honest about what doesn't work.

---

## What they're scoring

- Thoughtful, creative approach
- Facts actually grounded in the PDFs
- Sensible handling of ambiguity and uncertainty
- Generalizes beyond the starter docs
- Clear engineering decisions and trade-offs

Explicit: **"A smaller, understandable prototype is better than a large system whose behavior is unclear."** They are telling you not to over-build.

---

## Brownie points (optional, only after the core works)

- Large PDFs without perf collapse
- Many PDFs in one knowledge layer
- Schema that grows as new fact types appear
- **Incremental ingestion** — add a document without recomputing everything

Or any other extension that genuinely improves the core.

---

## Deliverables checklist

GitHub repo with a `README.md` containing exactly these sections:

- **Setup and Run Instructions**
- **Video Demo** (link, ≤ 3 min, shows a PDF processed + the four cases)
- **Approach** — architecture, decisions, trade-offs, **and which AI tools you used**
- **Limitations and Next Steps**
- **Additional Notes**

Submit repo link + video link via their Google Form.

---

## What's genuinely open

Language, framework, database, LLM, schema, storage format, output format, UI vs API. All your call. The only fixed points are the five requirements, the four cases, and the README structure.

The real difficulty is **case 3** — distinguishing "these numbers differ because one is Q3 and one is FY" from "these numbers differ because someone is wrong." That requires capturing *qualifiers* (period, scope, unit, currency, as-of date) alongside every fact value, which shapes your entire fact schema. Design for that first.

---

## Turn 2 — 2026-09-07 13:05

**Me:**

> gh repo clone mintnchocochip/superjoin-assignment pull from origin and keep these files as well. Or push these files to that repo and clone

*[Claude ran 8 tools: Bash ×8]*

**Claude:**

Cloning into a non-empty dir won't work directly. I'll init here, add the remote, and check out — that keeps your PDFs.

Repo is connected and merged with your local files. Now committing and pushing.

Done. `A:\superjoin-proj` is now the repo working tree, on `main`, tracking origin, everything pushed.

```
582d854 Add assignment brief and starter datasets   ← pushed
31d9beb Initial commit                              ← from origin (README.md)
```

**What's here now:**
- `README.md` — pulled from origin (currently just the title line)
- `starter-datasets/` — the six PDFs, committed
  - `delhivery/` — prospectus 2022, annual report FY24, Q4 FY24 earnings deck
  - `india-macroeconomy/` — Economic Survey 24-25, RBI annual report 24-25, IMF Article IV 2025
- `superjoin-vit-2026-assignment.pdf` — committed
- `starter-datasets.zip` — gitignored (redundant with the extracted tree)

Two notes:

`[redacted].pdf` (the bonafide certificate) is no longer in the folder — it disappeared between my first read and this turn, so it wasn't mine to delete or commit. If you want it back it'll need restoring from wherever you moved it; it's unrelated to the assignment anyway, and personal, so leaving it out of a public repo is the right call.

You got **two** datasets of three PDFs, not one. That's a gift for the assignment — the four required cases will be much easier to find in `india-macroeconomy/` (three institutions publishing overlapping GDP/inflation figures with different vintages = contradictions and time-scope reconciliations practically pre-made), while `delhivery/` gives you the entity-style facts (directors, addresses, revenue across periods). Building against both is also your generalization proof.

---

## Turn 3 — 2026-09-07 13:31

**Me:**

> i need to keep a track of all the prompts and response i am giving claude ffor transperancy

*[Claude ran 18 tools: Bash ×14, AskUserQuestion ×1, Write ×1, ToolSearch ×1, ExitPlanMode ×1]*

**Claude:**

Ponytail check first: Claude Code already records every session transcript. Let me verify before proposing anything that writes logs.

Transcripts already exist — no logging system needed. Let me understand the record shape.

Structure is clean. One concern before designing — checking what personal data would end up in a public transcript.

I have what I need — no codebase to explore (the repo is just a README and PDFs), and I've traced the exact data source. Two decisions change the shape of the work:

Decisions locked. Writing the plan.

Turn count differs from my earlier baseline (karpathy: 3 vs 2). Verifying rather than assuming.

Not a bug — turn 3 (`give me a system architecture for the same`) landed in that session after my baseline scan. Now verifying redaction:

Content intact, identifiers gone. Idempotency check:

---
