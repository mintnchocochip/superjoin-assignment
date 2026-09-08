# Evidence mining pipeline setup

Session `a9891da4` · 5 turns

## Turn 1 — 2026-09-08 14:48

**Me:**

> superjoin-proj on  evidence-mining-and-labelling [$!] via  v3.14.4 
> ❯ cd deploy && sudo docker compose --env-file ../.env down -v && cd ..
> python3 run_pipeline.py --setup-only
> [sudo: authenticate] Password:         
> [+] down 7/7
>  ✔️ Container facts-mongo3             Removed                                                                  0.1s
>  ✔️ Container facts-mongo1             Removed                                                                  0.1s
>  ✔️ Container facts-mongo2             Removed                                                                  0.1s
>  ✔️ Volume superjoin-facts_mongo2-data Removed                                                                  0.0s
>  ✔️ Volume superjoin-facts_mongo3-data Removed                                                                  0.0s
>  ✔️ Volume superjoin-facts_mongo1-data Removed                                                                  0.0s
>  ✔️ Network superjoin-facts_default    Removed                                                                  0.1s
> ==> creating .venv
> ==> re-running inside /home/irk/Desktop/project run/mani/superjoin-proj/.venv/bin/python
> ==> starting the Docker daemon
> ==> using sudo for docker (group membership needs a re-login)
> ==> installing Python dependencies
> ==> starting MongoDB replica set
>     normalising line endings in bootstrap.sh
> ==> starting three mongod nodes
> [+] up 18/18
>  ✔️ Image mongo:7                      Pulled                                                                  45.5s
>  ✔️ Volume superjoin-facts_mongo3-data Created                                                                  0.0s
>  ✔️ Volume superjoin-facts_mongo2-data Created                                                                  0.0s
>  ✔️ Network superjoin-facts_default    Created                                                                  0.0s
>  ✔️ Volume superjoin-facts_mongo1-data Created                                                                  0.0s
>  ✔️ Container facts-mongo3             Started                                                                  0.9s
>  ✔️ Container facts-mongo2             Started                                                                  1.1s
>  ✔️ Container facts-mongo1             Started                                                                  1.0s
> ==> waiting for mongo1 to accept authenticated connections
> ==> initiating replica set
> initiated
> ==> waiting for a primary
> ==> applying roles and users
> rs0 [direct: primary] test> 
> rs0 [direct: primary] test> 
> rs0 [direct: primary] test> 
> rs0 [direct: primary] test> 
> rs0 [direct: primary] test> 
> rs0 [direct: primary] test> 
> rs0 [direct: primary] test> 
> rs0 [direct: primary] test> 
> rs0 [direct: primary] test> 
> rs0 [direct: primary] test> 
> rs0 [direct: primary] test> 
> rs0 [direct: primary] test> 
> rs0 [direct: primary] test> 
> rs0 [direct: primary] test> 
> rs0 [direct: primary] test> 
> rs0 [direct: primary] test> 
> rs0 [direct: primary] test> 
> rs0 [direct: primary] test> 
> rs0 [direct: primary] test> 
> rs0 [direct: primary] test> 
> rs0 [direct: primary] factlayer> 
> rs0 [direct: primary] factlayer> | | | | | | | | | | [Function: upsertRole]
> rs0 [direct: primary] factlayer> 
> rs0 [direct: primary] factlayer> | | | | | | | | | | [Function: upsertUser]
> rs0 [direct: primary] factlayer> 
> rs0 [direct: primary] factlayer> | | | | | | | created role factsWriter
>
> rs0 [direct: primary] factlayer> 
> rs0 [direct: primary] factlayer> | | | | | | created role factsReader
>
> rs0 [direct: primary] factlayer> 
> rs0 [direct: primary] factlayer> created user facts_app
>
> rs0 [direct: primary] factlayer> created user facts_ro
>
> rs0 [direct: primary] factlayer> 
> rs0 [direct: primary] factlayer> 
> RBAC applied on factlayer. Users: facts_app, facts_ro
>
> rs0 [direct: primary] factlayer> ==> replica set members
>    mongo1:27017  PRIMARY
>    mongo2:27017  SECONDARY
>    mongo3:27017  SECONDARY
>
> Replica set rs0 is up on 27017/27018/27019. TLS required, RBAC on.
> The application connection string is in ../.env (gitignored).
> ==> checking Ollama at http://127.0.0.1:12345
> ==> starting the Ollama service
>
> [stop] Ollama is not answering at http://127.0.0.1:12345
>        Start it with ollama serve. On Windows the default port 11434 can fall inside a reserved range once Docker is running - if it refuses to bind, run OLLAMA_HOST=127.0.0.1:12345 ollama serve and put OLLAMA_HOST=127.0.0.1:12345 in .env.
>
> superjoin-proj on  evidence-mining-and-labelling [$!] via  v3.14.4 took 2m12s 
> Ensure that all of the deps and evth works properly on windows 11 as well as Ubuntu 26.04 LTS

*[Claude ran 8 tools: Bash ×8]*

---

## Turn 2 — 2026-09-08 15:37

**Me:**

> start running locally using the pdfs, i would atleast have something to show for

*[Claude ran 39 tools: Bash ×33, PowerShell ×5, AskUserQuestion ×1]*

**Claude:**

Cleaned up the WSL detour — no containers or volumes left behind there.

**Where things stand on this machine:**

| Piece | State |
|---|---|
| `.venv` + deps (fastapi, pymongo, pymupdf, uvicorn) | ✅ installed, survives the reboot |
| Ollama + `qwen3:4b` | ✅ installed; serves fine on `127.0.0.1:12345` |
| `.env` (MONGO_URI, OLLAMA_HOST) | ✅ correct for Windows |
| Docker Desktop | ❌ backend crashes: `sailor-ingest.sock` can't be renamed — orphaned AF_UNIX sockets in `AppData\Local\Docker\run` that nothing can delete until a reboot; `com.docker.service` is also Stopped and needs an elevated start |

After the reboot, Docker Desktop should come up clean (it starts the service itself as admin). Then one command does the rest — it creates nothing new, just brings up the replica set, starts `ollama serve` on 12345, ingests the six PDFs and labels:

```bash
cd /a/superjoin-proj && python run_pipeline.py --yes
```

Full labelling is ~1,500 model calls at ~40s each, so for something showable sooner:

```bash
cd /a/superjoin-proj && python run_pipeline.py --yes --limit 25
```

It's resumable — already-labelled evidence is skipped, so the capped run then the full run costs nothing extra. Ping me when you're back up and I'll drive it and check the output.

One thing I found before you redirected me, worth fixing separately: your Ubuntu box's `.env` was copied from here, so it carries `OLLAMA_HOST=127.0.0.1:12345` (a Windows-only workaround — systemd Ollama listens on 11434) and a `tlsCAFile=A:/...` path that doesn't exist on Linux. That's exactly why the Ubuntu run stopped at "Ollama is not answering".

---

## Turn 3 — 2026-09-08 16:12

**Me:**

> gang the llm is not labelling textual data and other data. The model is taking only 5gb of VRAM. Use rest of the gpu for context gng.

*[Claude ran 38 tools: Bash ×26, Edit ×10, PowerShell ×2]*

**Claude:**

Right — let me confirm the text-labelling failure before touching anything. Ollama's up here, and `label.py` runs standalone without Mongo.

Now the fix. First, the text-labelling path:

Now the other half of "not labelling other data" — numeric rows are gated by a metric whitelist:

Two separate causes, both fixed.

**1. Prose was never given prose instructions.** `SYSTEM` opens with *"You label a single number that has already been extracted"* — and a text row arrives as `CANDIDATE NUMBER: (none - this is a text excerpt)`. The model does the consistent thing with that: `is_fact: false`, every field null. Mining already yields text rows and `worth_labelling` already keeps them; only the instruction was missing. [label.py:148](label.py:148) adds `TEXT_SYSTEM` (metric = what the sentence asserts, is_fact rules rewritten for statements, two prose examples), [label.py:378](label.py:378) gives text rows a sentence-shaped prompt, and `label_one` routes on `kind`.

**2. Numbers were gated by a 20-entry whitelist.** `worth_labelling` dropped any figure whose row label didn't match `TARGETS` — that's your "other data". `LABEL_ALL=1` now labels every mined row that names anything (a figure with no row label at all is still a page number, still skipped).

**3. GPU knobs**, all env-tunable: `OLLAMA_NUM_CTX` (default raised 4096 → 16384), `LABEL_WORKERS` (was hardcoded 4), and `keep_alive: 30m` so the model stops being evicted between documents.

```bash
OLLAMA_NUM_PARALLEL=8 OLLAMA_KEEP_ALIVE=30m ollama serve
```
```bash
LABEL_ALL=1 LABEL_WORKERS=8 OLLAMA_MODEL=qwen3:14b python run_pipeline.py --yes
```

Straight answer on the VRAM though: **context alone won't spend it.** A call is a section header, a row label, one source line and three lines of context — about 500 tokens. 16k of context just reserves KV cache it never fills. What actually uses an idle card is `OLLAMA_NUM_PARALLEL` + `LABEL_WORKERS` (throughput, and 4 slots × 16k is where the VRAM goes) and a bigger `OLLAMA_MODEL` — qwen3:14b at ~9GB will label prose meaningfully better than the 4b will. `OLLAMA_NUM_PARALLEL` has to be set on the *server*, not the client; under systemd that's `sudo systemctl edit ollama` → `Environment="OLLAMA_NUM_PARALLEL=8"`.

One catch: claims are keyed by evidence_id and resumed runs skip anything already labelled, so switching to 14b relabels **nothing** already done — it only picks up the newly-eligible text and unlisted rows. Say the word if you want the existing claims dropped so the bigger model redoes them.

Verified: `python label.py` self-check passes, and I confirmed the routing by capturing what `label_one` sends (text → `TEXT_SYSTEM`, numbers → unchanged `SYSTEM`). The live model half I could **not** run here — this box's `qwen3:4b` blob is corrupt (`unable to load model`) and its Ollama is 0.6.1, too old to re-pull it. On your GPU box `python label.py` runs that half and asserts a sentence comes back labelled rather than blank — worth running there before the full pass.

---

## Turn 4 — 2026-09-08 16:30

**Me:**

> gang labelling is shit, numbers only its taking. Correlation and corroboration is even worse. everything is fucked. THe model is thinking like a monkey. Kindly modify the prompt and other supporting toolschains to properly label gng i need to submit it tmrw mrng

*[Claude ran 41 tools: Edit ×23, Bash ×18]*

**Claude:**

Now the prose prompt gets the same treatment, plus its own vocabulary:

Now the vocabularies and the two schemas, right after `TARGETS`:

Now store: group on the bucket, not the phrasing.

Two more correlation killers while I'm here — period strings that never match:

Let me get real numbers on what the prefilter now admits, straight from the PDFs:

Fixed three things, all of which were upstream of the model. Offline proof at the end.

**1. Grouping keyed off the model's phrasing.** `group_key = corpus | slug(metric)` where `metric` was free text. The annual report says "Revenue from operations", the deck says "Total income", the survey says "Revenue" — three groups of one, and a group of one corroborates nothing. That is why correlation looked worse than the labelling. There's now a `metric_key` enum (the 20 `TARGETS` buckets for figures, an 11-event vocabulary for prose) that constrained decoding forces the model to pick from — and for figures the deterministic `target_metric(row_label)` overrides it entirely, so the model doesn't get a vote where the row label already settles it. `"other"` deliberately never becomes a key; it falls through to the wording, because one giant "other" group adjudicating against itself is worse than no group.

**2. Two spellings of one period.** Column header gave `FY2024`, the model — told to quote the page — gave `"year ended March 31, 2024"`. `compare_qualifiers` read that as a *difference*, so genuine corroborations came out as "reconcilable: one covers X, the other Y". The model's period now goes through the same `period_from()` parser as the header.

**3. Prose still needs the earlier fix to be live.** `TEXT_SYSTEM`/`TEXT_SCHEMA` from the last turn — check you actually have them on the Ubuntu box, because that code is sitting uncommitted in this repo and your run is on a copy at `~/Desktop/project run/mani/`.

```
group keys : {'delhivery-limited|revenue'}     # was 3 distinct keys
periods    : {'FY2024'}                        # was 2 spellings
verdict    : corroborates | same value stated in crore and million
```

**Run plan, and one warning.** I mined all six PDFs to get real counts:

| | numbers | text | default prefilter | `LABEL_ALL=1` |
|---|---:|---:|---:|---:|
| all six docs | 14,578 | 8,181 | **3,184 calls** | 21,196 calls |

At ~40s a call, 8-way parallel: default ≈ **4.5 hours**, `LABEL_ALL=1` ≈ **29 hours**. So do *not* set `LABEL_ALL` tonight — and stay on qwen3:4b, since 14b is 2-3× slower and you don't have the hours. Parallelism is the only GPU lever that helps before morning.

```bash
python label.py && python run_pipeline.py --relabel --yes --limit 5
```

That's ~30 calls, five minutes — look at the findings before committing to the night. `label.py` runs a live check on the GPU box that asserts a sentence comes back labelled rather than blank (it skipped here: this box's qwen3:4b blob is corrupt and its Ollama is 0.6.1, too old to re-pull, so the live half is genuinely unverified by me — the offline checks all pass).

Then the real run. `--relabel` drops existing claims first; without it every changed prompt is invisible, because resume skips evidence that already has a claim:

```bash
LABEL_WORKERS=8 python run_pipeline.py --relabel --yes
```

with `OLLAMA_NUM_PARALLEL=8` on the *server* side. If the card is 8GB, drop both to 4 and set `OLLAMA_NUM_CTX=8192` — spilling to CPU would cost you more than the parallelism buys.

Want me to commit this to `evidence-mining-and-labelling` and push, so you can pull it on the Ubuntu box? Say the word and it's done in one command.

---

## Turn 5 — 2026-09-08 16:45

**Me:**

> yes commit and push

*[Claude ran 7 tools: Bash ×7]*

**Claude:**

Pushed — three commits on `evidence-mining-and-labelling`:

| | |
|---|---|
| `8f28109` | Require a figure to state its unit somewhere (your extract.py work, was uncommitted) |
| `5f5ea71` | Group claims by metric bucket, prose gets its own prompt |
| `052053f` | Update the AI transparency log |

On the Ubuntu box:

```bash
git pull origin evidence-mining-and-labelling
```

Then, before you start the long run, two things that will otherwise cost you the night:

```bash
grep -E 'OLLAMA_HOST|tlsCAFile' .env
```

That `.env` was copied from the Windows box, so it likely still says `OLLAMA_HOST=127.0.0.1:12345` (systemd Ollama listens on **11434**) and `tlsCAFile=A:/...` (a path that doesn't exist on Linux). Fix both before running — that's the same failure that stopped your first setup.

Then the five-minute sanity pass, then the real thing:

```bash
python label.py && python run_pipeline.py --relabel --yes --limit 5
```

```bash
LABEL_WORKERS=8 python run_pipeline.py --relabel --yes
```

~3,184 calls, roughly 4.5 hours at 8-way parallel. Check the findings after the `--limit 5` pass and ping me if the labels still look wrong — there's time to iterate on the prompt before you commit to the full run.

---
