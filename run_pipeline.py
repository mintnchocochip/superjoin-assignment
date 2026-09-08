#!/usr/bin/env python3
"""Run the whole pipeline on this machine and hand back a database dump.

Written so someone else can do the slow half on faster hardware. Mining is fast;
labelling is roughly forty seconds per model call and there are on the order of
1,500 calls across the six starter documents, so it wants a real GPU and a few
hours rather than a laptop and an afternoon.

    python run_pipeline.py                 # everything, then write a dump
    python run_pipeline.py --yes           # same, no confirmation prompts
    python run_pipeline.py --limit 50      # cap model calls per document
    python run_pipeline.py --pdfs some/dir # ingest a different folder
    python run_pipeline.py --restore f.zip # load a dump someone sent back

It is safe to stop and re-run. Claims are keyed by evidence id and already
labelled evidence is skipped, so a second run resumes where the first stopped
rather than starting over or duplicating anything.
"""

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import zipfile

ROOT = pathlib.Path(__file__).parent
DUMP_DIR = ROOT / "dump"
COLLECTIONS = ("corpora", "pdfs", "evidence", "claims", "claim_groups")


def say(msg):
    print(msg, flush=True)


def die(msg, hint=""):
    say(f"\n[stop] {msg}")
    if hint:
        say(f"       {hint}")
    sys.exit(1)


def run(cmd, **kw):
    return subprocess.run(cmd, cwd=ROOT, **kw)


# --- setup -----------------------------------------------------------------

def install_dependencies():
    say("==> installing Python dependencies")
    if run([sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"]).returncode:
        die("pip install failed")


def start_database():
    """Bring up the replica set. bootstrap.sh is idempotent, so this is cheap."""
    say("==> starting MongoDB replica set")
    if not shutil.which("docker"):
        die("docker is not installed", "Install Docker Desktop, then start it before re-running.")
    if run(["docker", "info"], stdout=subprocess.DEVNULL,
           stderr=subprocess.DEVNULL).returncode:
        die("the Docker daemon is not running", "Start Docker Desktop, then re-run this script.")

    bash = shutil.which("bash")
    if not bash:
        die("bash is not available",
            "On Windows, run this from Git Bash. bootstrap.sh needs a POSIX shell.")
    if run([bash, "deploy/bootstrap.sh"]).returncode:
        die("deploy/bootstrap.sh failed", "Its output above says why.")


def load_env():
    """bootstrap.sh writes .env; the modules read it at import time."""
    env = ROOT / ".env"
    if not env.exists():
        die(".env was not created", "deploy/bootstrap.sh should have written it.")
    for line in env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def ensure_model():
    """Ollama must be reachable AND able to load the model - those differ."""
    import label as labeller

    say(f"==> checking Ollama at {labeller.HOST}")
    if labeller.MODEL not in labeller.available():
        if not shutil.which("ollama"):
            die(f"Ollama is not reachable at {labeller.HOST}",
                "Install it from ollama.com, run `ollama serve`, then re-run.")
        say(f"    pulling {labeller.MODEL} (a few GB, once)")
        if run(["ollama", "pull", labeller.MODEL]).returncode:
            die(f"could not pull {labeller.MODEL}")

    if not labeller.available():
        die(f"Ollama is not answering at {labeller.HOST}",
            "Start it with `ollama serve`. On Windows the default port 11434 can "
            "fall inside a reserved range once Docker is running - if it refuses "
            "to bind, run `OLLAMA_HOST=127.0.0.1:12345 ollama serve` and put "
            "OLLAMA_HOST=127.0.0.1:12345 in .env.")

    # Answering is not the same as being able to generate: a corrupt blob returns
    # HTTP 500 per request and would otherwise look like a very slow run.
    if err := labeller.probe():
        die(f"{labeller.MODEL} cannot generate", err)
    say(f"    {labeller.MODEL} loads and generates")


# --- pipeline --------------------------------------------------------------

def ingest_documents(pdf_dir):
    import app
    import store

    pdfs = sorted(pathlib.Path(pdf_dir).rglob("*.pdf"))
    if not pdfs:
        die(f"no PDFs under {pdf_dir}")

    say(f"==> ingesting {len(pdfs)} PDFs from {pdf_dir}")
    store.ensure_indexes()
    for path in pdfs:
        result = app.ingest(path.read_bytes(), path.name)
        if result.get("duplicate"):
            say(f"    {path.name[:52]:<54} already ingested")
            continue
        say(f"    {path.name[:52]:<54} {result['accepted']:>5} evidence "
            f"-> {result.get('corpus_name') or '(unidentified)'} / {result['doc_type']}")


def confirm_corpora(assume_yes):
    """Grouping happens before labelling, and a human signs off on the grouping.

    The model proposes; this is where someone agrees. Getting it wrong is cheap
    to correct later - reassignment re-keys claims without another model call -
    but getting it wrong silently is not, which is why it asks.
    """
    import store

    pending = [d for d in store.list_pdfs() if not d.get("corpus_confirmed")]
    if not pending:
        say("==> all documents already assigned to a corpus")
        return

    say(f"==> confirming the corpus for {len(pending)} documents")
    for doc in pending:
        proposed = doc.get("corpus_name") or doc.get("proposed_subject")
        if not proposed:
            say(f"    SKIPPED {doc['filename'][:48]} - nothing proposed; assign it in the UI")
            continue

        if assume_yes:
            answer = ""
        else:
            say(f"\n    {doc['filename']}")
            say(f"      proposed entity : {proposed}")
            say(f"      document type   : {doc.get('doc_type')}")
            answer = input("      accept? [Y/n, or type the correct entity] ").strip()

        if answer.lower() in ("n", "no"):
            say("      left unconfirmed; it will not be labelled")
            continue
        name = "" if answer.lower() in ("", "y", "yes") else answer
        corpus = store.find_or_create_corpus(name) if name else {"_id": doc["corpus_id"]}
        store.set_corpus(doc["id"], corpus["_id"], confirmed=True)
        say(f"      -> {corpus['_id']}")


def label_documents(limit):
    import app
    import store

    docs = [d for d in store.list_pdfs() if d.get("corpus_confirmed")]
    if not docs:
        die("no confirmed documents to label")

    say(f"\n==> labelling {len(docs)} documents (~40s per model call)")
    for doc in docs:
        pending = app._pending(doc["id"])
        calls = len(app._call_groups(pending))
        if not calls:
            say(f"    {doc['filename'][:48]:<50} nothing left to label")
            continue

        planned = min(calls, limit)
        say(f"    {doc['filename'][:48]:<50} {planned} calls "
            f"(~{planned * 40 / app.WORKERS / 60:.0f} min)")
        started = time.time()
        written = app._label_batch(doc["id"], limit)
        say(f"      {written} claims in {(time.time() - started) / 60:.1f} min")


def adjudicate():
    import store

    say("\n==> adjudicating")
    store.sync_groups()
    groups = store.adjudicate_groups()
    counts = {}
    for row in store.findings(limit=100000):
        counts[row["verdict"]["type"]] = counts.get(row["verdict"]["type"], 0) + 1
    say(f"    {groups} groups | findings: {counts or 'none'}")


# --- dump and restore ------------------------------------------------------

def write_dump():
    """Export every collection as JSONL and zip it.

    Deliberately not mongodump: the official server image does not always carry
    the database tools, and a dump that cannot be produced on the machine that
    has the data is no dump at all. json_util keeps ObjectIds and datetimes
    round-trippable, and restore() is the matching half.
    """
    import store
    from bson import json_util

    if DUMP_DIR.exists():
        shutil.rmtree(DUMP_DIR)
    DUMP_DIR.mkdir()

    say("\n==> writing dump")
    total = 0
    for name in COLLECTIONS:
        path = DUMP_DIR / f"{name}.jsonl"
        count = 0
        with path.open("w", encoding="utf-8") as handle:
            for doc in store.db()[name].find():
                handle.write(json_util.dumps(doc) + "\n")
                count += 1
        total += count
        say(f"    {name:<14} {count:>7} documents")

    archive = ROOT / "factlayer-dump.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(DUMP_DIR.iterdir()):
            zf.write(path, path.name)
        zf.writestr("MANIFEST.json", json.dumps(
            {"collections": list(COLLECTIONS), "documents": total,
             "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, indent=2))
    shutil.rmtree(DUMP_DIR)

    size = archive.stat().st_size / 1e6
    say(f"\nDone. Send back: {archive}  ({size:.1f} MB)")


def restore(archive):
    """Load a dump produced by write_dump. Replaces documents by _id."""
    import store
    from bson import json_util
    from pymongo import ReplaceOne

    load_env()
    store.ensure_indexes()
    say(f"==> restoring from {archive}")
    with zipfile.ZipFile(archive) as zf:
        for name in COLLECTIONS:
            entry = f"{name}.jsonl"
            if entry not in zf.namelist():
                continue
            ops = []
            for line in zf.read(entry).decode("utf-8").splitlines():
                if line.strip():
                    doc = json_util.loads(line)
                    ops.append(ReplaceOne({"_id": doc["_id"]}, doc, upsert=True))
            if ops:
                store.db()[name].bulk_write(ops, ordered=False)
            say(f"    {name:<14} {len(ops):>7} documents")
    say("Done.")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pdfs", default="starter-datasets",
                        help="folder of PDFs to ingest (default: starter-datasets)")
    parser.add_argument("--limit", type=int, default=100000,
                        help="cap model calls per document")
    parser.add_argument("--yes", action="store_true",
                        help="accept every proposed corpus without asking")
    parser.add_argument("--restore", metavar="ZIP",
                        help="load a dump instead of running the pipeline")
    parser.add_argument("--skip-setup", action="store_true",
                        help="assume dependencies, database and model are ready")
    args = parser.parse_args()

    if args.restore:
        return restore(args.restore)

    if not args.skip_setup:
        install_dependencies()
        start_database()
    load_env()
    if not args.skip_setup:
        ensure_model()

    ingest_documents(args.pdfs)
    confirm_corpora(args.yes)
    label_documents(args.limit)
    adjudicate()
    write_dump()


if __name__ == "__main__":
    main()
