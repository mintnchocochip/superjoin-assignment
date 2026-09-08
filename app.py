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

    # Group before collecting. The model proposes which entity this document is
    # about from its cover pages, grounded in a quote, and the proposal is
    # matched against corpora that already exist so a second document about the
    # same company joins the first instead of starting a namespace of its own.
    # Nothing is labelled until a human confirms it.
    identity = labeller.identify(extract.cover_text(path)) or {}
    corpus = store.find_or_create_corpus(identity.get("subject"))

    store.insert_pdf({
        "_id": doc_id,
        "filename": file.filename,
        "sha256": digest,
        "page_count": pages,
        "uploaded_at": datetime.now(timezone.utc),
        "corpus_id": corpus["_id"] if corpus else None,
        "corpus_confirmed": False,
        "doc_type": identity.get("doc_type") or "other",
        "proposed_subject": identity.get("subject") or "",
        "proposed_evidence": identity.get("subject_evidence") or "",
    })
    store.insert_evidence([dict(r, doc_id=doc_id) for r in rows])

    return {
        "doc_id": doc_id,
        "duplicate": False,
        "mined": len(rows),
        "accepted": sum(1 for r in rows if r["accepted"]),
        "corpus_id": corpus["_id"] if corpus else None,
        "corpus_name": corpus["name"] if corpus else None,
        "doc_type": identity.get("doc_type") or "other",
        "proposed_evidence": identity.get("subject_evidence") or "",
        "needs_confirmation": True,
    }


@app.get("/api/corpora")
def corpora():
    return store.list_corpora()


@app.post("/api/documents/{doc_id}/remine")
def remine(doc_id: str):
    """Re-run extraction over a document already ingested.

    Needed because sha256 dedup refuses a re-upload, so without this a miner
    improvement could only reach documents ingested after it. Evidence ids are
    content-derived and do not move, so existing claims stay attached to their
    evidence and no labelling is repeated - a new column band simply appears on
    rows that were missing it.
    """
    pdf = store.get_pdf(doc_id)
    if not pdf:
        raise HTTPException(404, f"no document {doc_id}")
    path = UPLOADS / f"{doc_id}.pdf"
    if not path.exists():
        raise HTTPException(410, f"the uploaded file for {doc_id} is no longer on disk")

    _, rows = extract.mine_document(path, doc_id)
    written = store.insert_evidence([dict(r, doc_id=doc_id) for r in rows])
    return {"doc_id": doc_id, "mined": len(rows), "written": written}


@app.post("/api/documents/{doc_id}/corpus")
def assign_corpus(doc_id: str, name: str = "", doc_type: str = ""):
    """Confirm the proposed corpus, or reassign the document to another one.

    Re-keys the document's existing claims in place. No model call: which entity
    a document is about is a judgement that gets revised, and revising it must
    not cost another labelling run.
    """
    pdf = store.get_pdf(doc_id)
    if not pdf:
        raise HTTPException(404, f"no document {doc_id}")

    if name:
        corpus = store.find_or_create_corpus(name)
    elif pdf.get("corpus_id"):
        corpus = {"_id": pdf["corpus_id"]}
    else:
        raise HTTPException(400, "nothing proposed for this document; pass a name")

    if doc_type and doc_type not in labeller.DOC_TYPES:
        raise HTTPException(400, f"doc_type must be one of {sorted(labeller.DOC_TYPES)}")

    rekeyed = store.set_corpus(doc_id, corpus["_id"], doc_type or None, confirmed=True)
    return {"doc_id": doc_id, "corpus_id": corpus["_id"], "claims_rekeyed": rekeyed}


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

    # The corpus name is what the labeller is told the subject is, and the
    # corpus id is what the group key is built from. Neither comes from the
    # filename, and neither is the model's to choose.
    pdf = store.get_pdf(doc_id) or {}
    corpus = store.db().corpora.find_one({"_id": pdf.get("corpus_id")}) or {}
    corpus_name = corpus.get("name", "")
    for row in rows:
        row["subject"] = corpus_name

    batch = list(_call_groups(rows).values())[:limit]

    # Written as each group returns, not once at the end. A run that collected
    # everything and wrote it last lost all of it whenever the process died -
    # which happened three times over - and left the counter at zero throughout,
    # so a dead run and a slow one looked identical. Claims are keyed by
    # evidence_id and _pending() skips evidence that already has one, so an
    # interrupted run resumes from wherever it stopped.
    written = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for members, out in zip(batch, pool.map(labeller.label_one, [g[0] for g in batch])):
            if out is None:
                continue
            now = datetime.now(timezone.utc)
            written += store.upsert_claims([
                dict(
                    out,
                    _id=uuid.uuid4().hex[:16],
                    doc_id=doc_id,
                    corpus_id=pdf.get("corpus_id"),
                    doc_type=pdf.get("doc_type"),
                    subject=corpus_name,
                    evidence_id=row["id"],
                    period=labeller.period_from(row.get("col_header")) or out["period"],
                    labelled_at=now,
                )
                for row in members
            ])

    store.sync_groups(doc_id)
    # Deterministic and cheap, so it runs after every batch rather than being a
    # separate step somebody has to remember.
    store.adjudicate_groups()
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

    # Grouping comes first, and this is what enforces it. Labelling a document
    # under an unconfirmed corpus would write hundreds of claims under a subject
    # nobody checked, and every one of them would need re-keying afterwards.
    pdf = store.get_pdf(doc_id)
    if not pdf:
        raise HTTPException(404, f"no document {doc_id}")
    if not pdf.get("corpus_confirmed"):
        raise HTTPException(
            409,
            f"corpus not confirmed for this document "
            f"(proposed: {pdf.get('proposed_subject') or 'nothing'}). "
            f"POST /api/documents/{doc_id}/corpus first.",
        )

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
    produce a cross-document finding."""
    return [dict(g, group_key=g.pop("_id")) for g in store.cross_document_groups(limit)]


@app.post("/api/adjudicate")
def adjudicate(limit: int = 500):
    """Recompute every verdict. No model call, so this is always safe to re-run."""
    store.sync_groups()
    return {"groups_adjudicated": store.adjudicate_groups(limit)}


@app.get("/api/findings")
def findings(type: str = "", cross_document: int = 0, limit: int = 100):
    """Adjudicated pairs, each returned with both claims and their evidence.

    `type` filters to corroborates | contradicts | reconcilable. Omitted, it
    returns all three and excludes not_comparable, which is stored but is not a
    finding.
    """
    if type and type not in ("corroborates", "contradicts", "reconcilable",
                             "not_comparable"):
        raise HTTPException(400, f"unknown finding type {type!r}")

    rows = store.findings(type, bool(cross_document), limit)
    wanted = {r["verdict"][side] for r in rows for side in ("a", "b") if r["verdict"].get(side)}
    claims = store.claims_by_id(wanted)

    out = []
    for r in rows:
        v = r["verdict"]
        a, b = claims.get(v.get("a")), claims.get(v.get("b"))
        if not a or not b:
            continue
        out.append({
            "group_key": r["group_key"], "subject": r.get("subject"),
            "metric": r.get("metric"), "type": v["type"],
            "dimension": v.get("dimension"), "reason": v.get("reason"),
            "confidence": v.get("confidence"),
            "cross_document": v.get("cross_document"),
            "a": a, "b": b,
        })
    return out
