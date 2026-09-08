#!/usr/bin/env python3
"""Run the whole pipeline on this machine and hand back a database dump.

Written so someone else can do the slow half on faster hardware. Mining is fast;
labelling is roughly forty seconds per model call and there are on the order of
1,500 calls across the six starter documents, so it wants a real GPU and a few
hours rather than a laptop and an afternoon.

On Ubuntu this needs nothing but Python and sudo. It installs Docker Engine and
Ollama from their official installers, pulls the model, and runs everything in a
virtualenv it creates - so a system Python marked externally managed (PEP 668 on
24.04) and a docker group that is not live until the next login, which are the
two things that reliably derail a first run, are both handled rather than
explained.

    python run_pipeline.py                 # everything, then write a dump
    python run_pipeline.py --yes           # same, no confirmation prompts
    python run_pipeline.py --limit 50      # cap model calls per document
    python run_pipeline.py --pdfs some/dir # ingest a different folder
    python run_pipeline.py --restore f.zip # load a dump someone sent back
    python run_pipeline.py --setup-only    # install everything, then drive the UI
    python run_pipeline.py --dump-only     # zip up whatever is in the database

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


def quiet(cmd):
    """True if the command succeeds. Used for probing, so output is discarded."""
    return run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def have(name):
    return shutil.which(name) is not None


LINUX = sys.platform.startswith("linux")
DOCKER = ["docker"]  # becomes ["sudo", "docker"] when the group is not active yet


# --- provisioning ----------------------------------------------------------

def sudo_available():
    return have("sudo") and LINUX


def apt_install(*packages):
    say(f"    apt-get install {' '.join(packages)}")
    quiet(["sudo", "apt-get", "update", "-qq"])
    return run(["sudo", "apt-get", "install", "-y", "-qq", *packages]).returncode == 0


def ensure_venv():
    """Re-execute inside .venv, creating it if needed.

    Ubuntu 24.04 marks the system Python externally managed (PEP 668), so a plain
    `pip install` into it fails with an error most people then work around by
    force. A virtualenv sidesteps the question, and re-exec means the caller
    still only ran one command.
    """
    if sys.prefix != sys.base_prefix:
        return

    venv = ROOT / ".venv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        say("==> creating .venv")
        if run([sys.executable, "-m", "venv", str(venv)]).returncode:
            if LINUX and sudo_available():
                version = f"python{sys.version_info.major}.{sys.version_info.minor}"
                apt_install(f"{version}-venv", "python3-venv")
                if run([sys.executable, "-m", "venv", str(venv)]).returncode:
                    die("could not create a virtualenv",
                        f"Try: sudo apt-get install -y {version}-venv")
            else:
                die("could not create a virtualenv",
                    "Install the venv module for your Python, then re-run.")

    say(f"==> re-running inside {python}")
    os.execv(str(python), [str(python), __file__, *sys.argv[1:]])


def install_dependencies():
    say("==> installing Python dependencies")
    if run([sys.executable, "-m", "pip", "install", "-q", "--upgrade", "pip"]).returncode:
        say("    (could not upgrade pip; continuing)")
    if run([sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"]).returncode:
        die("pip install failed")


def install_docker():
    """Install Docker Engine from Docker's own convenience script.

    Only on Linux, and only when docker is genuinely absent. On macOS and
    Windows, Docker Desktop is a GUI application that cannot sensibly be
    installed from here, so those get an instruction instead.
    """
    if have("docker"):
        return
    if not LINUX:
        die("Docker is not installed",
            "Install Docker Desktop from docker.com, start it, then re-run.")
    if not sudo_available():
        die("Docker is not installed and sudo is unavailable",
            "Install it with: curl -fsSL https://get.docker.com | sudo sh")

    say("==> installing Docker Engine (get.docker.com, needs sudo)")
    have_curl = have("curl") or apt_install("curl")
    if not have_curl:
        die("curl is unavailable and could not be installed")
    script = ROOT / ".get-docker.sh"
    try:
        if run(["curl", "-fsSL", "-o", str(script), "https://get.docker.com"]).returncode:
            die("could not download the Docker installer")
        if run(["sudo", "sh", str(script)]).returncode:
            die("the Docker installer failed")
    finally:
        script.unlink(missing_ok=True)


def ensure_docker_running():
    """Start the daemon, and work around a group membership that is not live yet.

    `usermod -aG docker` does not affect the shell that ran it - the group only
    applies after a new login. Rather than stop and ask for a logout, which is
    exactly the tinkering this script exists to avoid, fall back to sudo for the
    rest of the run.
    """
    global DOCKER

    if LINUX and have("systemctl") and not quiet(["docker", "info"]):
        say("==> starting the Docker daemon")
        quiet(["sudo", "systemctl", "enable", "--now", "docker"])

    if quiet(["docker", "info"]):
        return
    if LINUX and sudo_available() and quiet(["sudo", "docker", "info"]):
        DOCKER = ["sudo", "docker"]
        say("==> using sudo for docker (group membership needs a re-login)")
        quiet(["sudo", "usermod", "-aG", "docker", os.environ.get("USER", "")])
        return

    die("the Docker daemon is not reachable",
        "Start Docker Desktop, or on Linux: sudo systemctl start docker")


def install_ollama():
    if have("ollama"):
        return
    if not LINUX:
        die("Ollama is not installed", "Install it from ollama.com, then re-run.")
    if not sudo_available():
        die("Ollama is not installed and sudo is unavailable",
            "Install it with: curl -fsSL https://ollama.com/install.sh | sh")

    say("==> installing Ollama (ollama.com/install.sh)")
    if not (have("curl") or apt_install("curl")):
        die("curl is unavailable and could not be installed")
    script = ROOT / ".install-ollama.sh"
    try:
        if run(["curl", "-fsSL", "-o", str(script), "https://ollama.com/install.sh"]).returncode:
            die("could not download the Ollama installer")
        if run(["sh", str(script)]).returncode:
            die("the Ollama installer failed")
    finally:
        script.unlink(missing_ok=True)


DEFAULT_OLLAMA = "http://127.0.0.1:11434"


def retune_ollama_host():
    """Fall back to Ollama's own default port when the configured one is dead.

    OLLAMA_HOST only ends up in .env to work around Windows, where 11434 sits
    inside a reserved TCP range Hyper-V claims once Docker Desktop starts. That
    value describes one machine. A project folder copied to another - which is
    how this gets handed to whoever has the faster GPU - carries it along, and
    the receiving Linux box then probes port 12345 forever while a perfectly
    healthy Ollama answers on 11434.

    So: if the configured host is silent and the default is not, believe the
    default and drop the stale line, since the uvicorn process reads .env too.
    """
    import label as labeller

    if labeller.available() or labeller.HOST == DEFAULT_OLLAMA:
        return
    stale, labeller.HOST = labeller.HOST, DEFAULT_OLLAMA
    if not labeller.available():
        labeller.HOST = stale
        return

    say(f"==> Ollama is on {DEFAULT_OLLAMA}, not {stale} (a stale OLLAMA_HOST,")
    say("    probably from a .env copied off another machine); correcting .env")
    os.environ["OLLAMA_HOST"] = DEFAULT_OLLAMA
    env = ROOT / ".env"
    kept = [ln for ln in env.read_text(encoding="utf-8").splitlines()
            if not ln.startswith("OLLAMA_HOST=")]
    env.write_text("\n".join(kept) + "\n", encoding="utf-8")


OLLAMA_LOG = ROOT / "ollama-serve.log"


def wait_for_ollama(tries):
    import label as labeller

    for _ in range(tries):
        if labeller.available():
            return True
        time.sleep(2)
    return False


def ensure_ollama_running():
    """Start Ollama by whichever route this machine actually offers.

    The official Linux installer registers a systemd unit, but a manual tarball
    or snap install does not, and `systemctl enable --now ollama` then fails with
    "unit ollama.service not found". That failure used to be discarded, so the
    run waited sixty seconds and died reporting Ollama silent - without ever
    trying the one thing that always works and needs no root: starting the server
    ourselves. Both routes now get a turn, and the server is detached so it
    outlives this script, because uvicorn needs it afterwards.
    """
    import label as labeller

    if labeller.available():
        return
    retune_ollama_host()
    if labeller.available():
        return

    if LINUX and have("systemctl"):
        say("==> starting the Ollama service")
        if quiet(["sudo", "systemctl", "enable", "--now", "ollama"]):
            if wait_for_ollama(15):
                return
        else:
            say("    no systemd unit for ollama; starting the server directly")

    say(f"==> starting ollama serve in the background ({OLLAMA_LOG.name})")
    with OLLAMA_LOG.open("ab") as log:
        subprocess.Popen(["ollama", "serve"], cwd=ROOT, stdout=log, stderr=log,
                         start_new_session=os.name != "nt")
    if wait_for_ollama(30):
        return

    # systemd starts ollama with its own environment, not this one, so a service
    # asked to move ports comes up on 11434 regardless. Check there before dying.
    retune_ollama_host()
    if not labeller.available() and OLLAMA_LOG.exists():
        say(f"\n    last lines of {OLLAMA_LOG.name}:")
        for line in OLLAMA_LOG.read_text(errors="replace").splitlines()[-12:]:
            say(f"    {line}")


def provision(assume_yes):
    """Install everything this needs. Asks first, because it touches the machine."""
    missing = [name for name in ("docker", "ollama") if not have(name)]
    if missing and not assume_yes:
        say("\nThis will install, with sudo: " + ", ".join(missing))
        say("  docker  <- https://get.docker.com")
        say("  ollama  <- https://ollama.com/install.sh")
        if input("Proceed? [Y/n] ").strip().lower() in ("n", "no"):
            die("nothing installed", "Install those two yourself, then re-run with --skip-setup.")

    install_docker()
    ensure_docker_running()
    install_ollama()


def fix_line_endings():
    """Rewrite CRLF shell scripts to LF.

    This repo is developed on Windows, and git will hand a checkout CRLF endings
    unless told otherwise. bash then reads the carriage return as part of the
    line and `set -euo pipefail` fails with "set: pipefail: invalid option name",
    which says nothing about the actual cause. .gitattributes prevents it for new
    clones; this repairs the ones that already exist, since asking someone to
    re-clone over an invisible byte is not a fix.
    """
    for script in sorted((ROOT / "deploy").glob("*.sh")):
        raw = script.read_bytes()
        if b"\r\n" in raw:
            say(f"    normalising line endings in {script.name}")
            script.write_bytes(raw.replace(b"\r\n", b"\n"))


def posix_bash():
    """A real POSIX bash, not the WSL launcher.

    On a default Windows install `where bash` finds C:\\Windows\\System32\\bash.exe,
    which is the WSL shim: it would run bootstrap.sh inside a Linux distro that
    has no Docker, no project at that path, and no visible connection to the
    error you get. Git for Windows ships the shell we actually want, but only
    puts git.exe on PATH, so look for it where it lives.
    """
    candidates = [shutil.which("bash")]
    if os.name == "nt":
        candidates += [rf"{root}\Git\bin\bash.exe" for root in
                       (os.environ.get("ProgramFiles", r"C:\Program Files"),
                        os.environ.get("ProgramFiles(x86)", ""),
                        os.environ.get("LOCALAPPDATA", "") + r"\Programs")]
    for path in candidates:
        if path and "System32" not in path and os.path.exists(path):
            return path
    return None


def start_database():
    """Bring up the replica set. bootstrap.sh is idempotent, so this is cheap."""
    say("==> starting MongoDB replica set")
    fix_line_endings()
    bash = posix_bash()
    if not bash:
        die("bash is not available",
            "On Windows, install Git for Windows - bootstrap.sh needs a POSIX shell."
            if os.name == "nt" else "Install bash, then re-run.")
    env = dict(os.environ, DOCKER=" ".join(DOCKER))
    if run([bash, "deploy/bootstrap.sh"], env=env).returncode:
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
    ensure_ollama_running()

    if not labeller.available():
        die(f"Ollama is not answering at {labeller.HOST}",
            "Start it with: sudo systemctl restart ollama" if LINUX else
            "Start it with `ollama serve`. If it refuses to bind, port 11434 is "
            "inside a reserved range Hyper-V claims once Docker Desktop starts - "
            "run `OLLAMA_HOST=127.0.0.1:12345 ollama serve` and put "
            "OLLAMA_HOST=127.0.0.1:12345 in .env.")

    if labeller.MODEL not in labeller.available():
        say(f"    pulling {labeller.MODEL} (a few GB, once)")
        if run(["ollama", "pull", labeller.MODEL]).returncode:
            die(f"could not pull {labeller.MODEL}")

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
    load_env()
    import store
    from bson import json_util
    from pymongo import ReplaceOne

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
    parser.add_argument("--setup-only", action="store_true",
                        help="install everything and stop, for driving the UI by hand")
    parser.add_argument("--dump-only", action="store_true",
                        help="write factlayer-dump.zip from whatever is in the database")
    args = parser.parse_args()

    if args.dump_only:
        load_env()
        return write_dump()

    if not args.skip_setup:
        ensure_venv()          # may re-exec; everything after runs in .venv

    # Restoring needs the database and the driver, but never the model, so it
    # skips provisioning Ollama and the several gigabytes that come with it.
    if args.restore:
        if not args.skip_setup:
            install_docker()
            ensure_docker_running()
            install_dependencies()
            start_database()
        return restore(args.restore)

    if not args.skip_setup:
        provision(args.yes)
        install_dependencies()
        start_database()
    load_env()
    if not args.skip_setup:
        ensure_model()

    if args.setup_only:
        say("\nReady. Start the interface with:\n"
            f"    {sys.executable} -m uvicorn app:app --port 8000\n"
            "then open http://localhost:8000 and drop the PDFs in.\n"
            "When you are done, write the dump with:\n"
            f"    {sys.executable} run_pipeline.py --dump-only")
        return

    ingest_documents(args.pdfs)
    confirm_corpora(args.yes)
    label_documents(args.limit)
    adjudicate()
    write_dump()


if __name__ == "__main__":
    main()
