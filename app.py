"""Upload PDFs, inspect the evidence mined from them, and label it with a local model.

Two writers, and the split between them is the point. Evidence is written once
at ingest, from the document, by regex. Claims are written by the model, keyed
by evidence id, so a mislabelled figure is still a correctly transcribed figure.

Storage is MongoDB. See store.py for the collections and where they differ from
the README schema, and deploy/ for the replica set they live in.
"""

import hashlib
import os
import pathlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).parent

# Load .env before store.py reads MONGO_URI at import time.
_ENV = ROOT / ".env"
if _ENV.exists():
    for _line in _ENV.read_text(encoding="utf-8").splitlines():
        if "=" in _line and not _line.lstrip().startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402

import extract  # noqa: E402
import label as labeller  # noqa: E402
import store  # noqa: E402

UPLOADS = ROOT / "uploads"

# Ollama is VRAM-bound; more than a handful of concurrent requests thrashes.
WORKERS = 4

app = FastAPI(title="Fact Knowledge Layer")


def subject_of(filename):
    """The entity a document is about. Given to the labeller, never inferred by it.

    ponytail: filename stem. Replace with a cover-page read when a document turns
    up whose name says nothing useful.
    """
    stem = pathlib.Path(filename).stem.replace("_", "-").lower()
    words = [w for w in stem.split("-") if not w.isdigit()]
    return " ".join(words[:3]) or stem


@app.on_event("startup")
def setup():
    UPLOADS.mkdir(exist_ok=True)
    store.ensure_indexes()


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
def health():
    """Model and database status, so a broken dependency says so instead of
    looking like slow progress."""
    try:
        topology = store.connect()
    except Exception as exc:
        topology = {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "model": {"configured": labeller.MODEL, "available": labeller.available()},
        "mongo": topology,
    }


@app.get("/api/models")
def models():
    return {"configured": labeller.MODEL, "available": labeller.available()}


@app.post("/api/documents")
async def upload(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted")

    blob = await file.read()
    digest = hashlib.sha256(blob).hexdigest()

    # A re-uploaded file would otherwise manufacture corroborations between a
    # document and its own copy once adjudication lands.
    existing = store.find_pdf_by_hash(digest)
    if existing:
        return {"doc_id": existing["_id"], "duplicate": True}

    doc_id = uuid.uuid4().hex[:12]
    path = UPLOADS / f"{doc_id}.pdf"
    path.write_bytes(blob)

    # Mining is regex over word positions: about a second for 100 pages, so it
    # stays inline. Labelling is the slow half and runs in the background.
    pages, rows = extract.mine_document(path, doc_id)

    store.insert_pdf({
        "_id": doc_id,
        "filename": file.filename,
        "subject": subject_of(file.filename),
        "sha256": digest,
        "page_count": pages,
        "uploaded_at": datetime.now(timezone.utc),
    })
    store.insert_evidence([dict(r, doc_id=doc_id) for r in rows])

    return {
        "doc_id": doc_id,
        "duplicate": False,
        "mined": len(rows),
        "accepted": sum(1 for r in rows if r["accepted"]),
    }


def _pending(doc_id):
    """Unlabelled evidence for a document that could actually reach a finding.

    The prefilter runs here rather than in the query because it is fuzzy.
    Pulling ten thousand documents out of Mongo costs milliseconds; forty
    seconds of model time on a row that can never be compared to anything costs
    the afternoon.
    """
    rows = store.pending_evidence(doc_id)
    return [r for r in rows if labeller.worth_labelling(r, r.get("subject", ""))]


def _call_groups(rows):
    """Group evidence into one model call each.

    Every figure on one printed row shares a section, a label and a source line,
    and differs only by column - which decides period, and period is derived
    deterministically. So one call answers the whole row. Keying on the source
    line rather than the label alone costs some reuse and buys correctness: the
    same label under two different statements is two different questions.
    """
    groups = {}
    for row in rows:
        key = (
            ("number", row.get("section"), row.get("row_label"), row["text"])
            if row["kind"] == "number"
            else ("text", row["id"])
        )
        groups.setdefault(key, []).append(row)
    return groups


def _label_batch(doc_id, limit):
    """Label pending evidence for a document. Writes only to `claims`."""
    rows = _pending(doc_id)
    if not rows:
        return 0

    batch = list(_call_groups(rows).values())[:limit]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        labelled = list(pool.map(labeller.label_one, [g[0] for g in batch]))

    now = datetime.now(timezone.utc)
    claims = []
    for members, out in zip(batch, labelled):
        if out is None:
            continue
        for row in members:
            claims.append(dict(
                out,
                _id=uuid.uuid4().hex[:16],
                doc_id=doc_id,
                evidence_id=row["id"],
                period=labeller.period_from(row.get("col_header")) or out["period"],
                labelled_at=now,
            ))

    written = store.upsert_claims(claims)
    store.sync_groups(doc_id)
    return written


@app.post("/api/documents/{doc_id}/label")
def start_labelling(doc_id: str, tasks: BackgroundTasks, limit: int = 400):
    if not labeller.available():
        raise HTTPException(503, f"Ollama is not reachable at {labeller.HOST}")
    # /api/tags answering is not proof the model loads. Ask it to generate once,
    # so a bad model fails here with its own message instead of silently
    # producing an empty run.
    if err := labeller.probe():
        raise HTTPException(503, f"{labeller.MODEL} cannot generate - {err}")

    pending = _pending(doc_id)
    tasks.add_task(_label_batch, doc_id, limit)
    return {
        "started": True,
        "model": labeller.MODEL,
        "pending": len(pending),
        "calls": min(len(_call_groups(pending)), limit),
    }


@app.get("/api/documents")
def documents():
    return store.list_pdfs()


@app.get("/api/documents/{doc_id}/evidence")
def evidence(doc_id: str, accepted: int = 1, kind: str = "", q: str = "",
             page: int = -1, labelled: int = -1, limit: int = 300):
    return store.find_evidence(doc_id, bool(accepted), kind, q, page, labelled, limit)


@app.get("/api/groups")
def groups(limit: int = 100):
    """Claim groups spanning more than one document - the only ones that can
    produce a finding, and what the adjudication step will read."""
    return [dict(g, group_key=g.pop("_id")) for g in store.cross_document_groups(limit)]
