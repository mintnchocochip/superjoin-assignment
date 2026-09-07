"""Upload PDFs, inspect the evidence mined from them, and label it with a local model.

Two tables, and the split between them is the point. `evidence` is written once
at ingest, from the document, by regex. `claims` is written by the model. The
model is handed an evidence id and returns labels keyed by it, so a mislabelled
figure is still a correctly transcribed figure.

Storage is SQLite for now. The architecture commits to MongoDB, but the schema
is still moving and a daemon is friction this early; the swap is the queries in
this file, and it happens once verdict shapes settle.
"""

import hashlib
import pathlib
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

import extract
import label as labeller

ROOT = pathlib.Path(__file__).parent
UPLOADS = ROOT / "uploads"
DB_PATH = ROOT / "facts.db"

# Ollama is VRAM-bound; more than a handful of concurrent requests thrashes.
WORKERS = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id           TEXT PRIMARY KEY,
    filename     TEXT NOT NULL,
    sha256       TEXT NOT NULL UNIQUE,
    page_count   INTEGER NOT NULL,
    uploaded_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    id            TEXT PRIMARY KEY,
    doc_id        TEXT NOT NULL,
    kind          TEXT NOT NULL,
    pdf_page      INTEGER NOT NULL,
    printed_page  TEXT,
    text          TEXT NOT NULL,
    row_label     TEXT,
    col_header    TEXT,
    value         TEXT,
    currency      TEXT,
    unit          TEXT,
    char_start    INTEGER,
    char_end      INTEGER,
    accepted      INTEGER NOT NULL,
    reject_reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_evidence_doc ON evidence(doc_id, accepted, kind);
CREATE TABLE IF NOT EXISTS claims (
    id           TEXT PRIMARY KEY,
    evidence_id  TEXT NOT NULL REFERENCES evidence(id),
    is_fact      INTEGER NOT NULL,
    subject      TEXT,
    metric       TEXT,
    period       TEXT,
    basis        TEXT,
    model        TEXT NOT NULL,
    labelled_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_claims_evidence ON claims(evidence_id);
"""

INSERT_EVIDENCE = (
    "INSERT OR IGNORE INTO evidence (id, doc_id, kind, pdf_page, printed_page, text,"
    " row_label, col_header, value, currency, unit, char_start, char_end, accepted,"
    " reject_reason) VALUES (:id, :doc_id, :kind, :pdf_page, :printed_page, :text,"
    " :row_label, :col_header, :value, :currency, :unit, :char_start, :char_end,"
    " :accepted, :reject_reason)"
)

app = FastAPI(title="Fact Knowledge Layer")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


@app.on_event("startup")
def setup():
    UPLOADS.mkdir(exist_ok=True)
    with db() as conn:
        conn.executescript(SCHEMA)


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/models")
def models():
    return {"configured": labeller.MODEL, "available": labeller.available()}


@app.post("/api/documents")
async def upload(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted")

    blob = await file.read()
    digest = hashlib.sha256(blob).hexdigest()

    with db() as conn:
        # A re-uploaded file would otherwise manufacture corroborations between a
        # document and its own copy once adjudication lands.
        existing = conn.execute(
            "SELECT id FROM documents WHERE sha256 = ?", (digest,)
        ).fetchone()
        if existing:
            return {"doc_id": existing["id"], "duplicate": True}

    doc_id = uuid.uuid4().hex[:12]
    path = UPLOADS / f"{doc_id}.pdf"
    path.write_bytes(blob)

    # Mining is regex over word positions: about a second for 100 pages, so it
    # stays inline. Labelling is the slow half and runs in the background.
    pages, rows = extract.mine_document(path, doc_id)

    with db() as conn:
        conn.execute(
            "INSERT INTO documents VALUES (?, ?, ?, ?, ?)",
            (
                doc_id,
                file.filename,
                digest,
                pages,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        conn.executemany(INSERT_EVIDENCE, rows)

    return {
        "doc_id": doc_id,
        "duplicate": False,
        "mined": len(rows),
        "accepted": sum(1 for r in rows if r["accepted"]),
    }


def _label_batch(doc_id, limit):
    """Label unlabelled evidence for a document. Writes only to `claims`."""
    with db() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT e.* FROM evidence e"
                " LEFT JOIN claims c ON c.evidence_id = e.id"
                " WHERE e.doc_id = ? AND e.accepted = 1 AND c.id IS NULL"
                " ORDER BY e.pdf_page LIMIT ?",
                (doc_id, limit),
            )
        ]
    if not rows:
        return 0

    # What a number is *about* depends on its row label, not on its value or its
    # column - period is derived from the header deterministically. So one call
    # per distinct label covers every figure in that row across every column,
    # which is roughly a third of the calls. Text excerpts are each unique.
    groups = {}
    for row in rows:
        key = ("number", row["row_label"]) if row["kind"] == "number" else ("text", row["id"])
        groups.setdefault(key, []).append(row)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        labelled = list(pool.map(labeller.label_one, [g[0] for g in groups.values()]))

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    claims = []
    for members, out in zip(groups.values(), labelled):
        if out is None:
            continue
        for row in members:
            claims.append(
                dict(
                    out,
                    id=uuid.uuid4().hex[:16],
                    evidence_id=row["id"],
                    period=labeller.period_from(row["col_header"]) or "",
                    labelled_at=now,
                )
            )

    with db() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO claims (id, evidence_id, is_fact, subject, metric,"
            " period, basis, model, labelled_at) VALUES (:id, :evidence_id, :is_fact,"
            " :subject, :metric, :period, :basis, :model, :labelled_at)",
            claims,
        )
    return len(claims)


@app.post("/api/documents/{doc_id}/label")
def start_labelling(doc_id: str, tasks: BackgroundTasks, limit: int = 200):
    if not labeller.available():
        raise HTTPException(503, "Ollama is not reachable at " + labeller.HOST)
    tasks.add_task(_label_batch, doc_id, limit)
    return {"started": True, "limit": limit, "model": labeller.MODEL}


@app.get("/api/documents")
def documents():
    with db() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT d.*,"
                "  (SELECT COUNT(*) FROM evidence e"
                "     WHERE e.doc_id = d.id AND e.accepted = 1) AS accepted,"
                "  (SELECT COUNT(*) FROM evidence e"
                "     WHERE e.doc_id = d.id AND e.accepted = 0) AS rejected,"
                "  (SELECT COUNT(*) FROM claims c JOIN evidence e ON e.id = c.evidence_id"
                "     WHERE e.doc_id = d.id) AS labelled,"
                "  (SELECT COUNT(*) FROM claims c JOIN evidence e ON e.id = c.evidence_id"
                "     WHERE e.doc_id = d.id AND c.is_fact = 1) AS facts"
                " FROM documents d ORDER BY d.uploaded_at DESC"
            )
        ]


@app.get("/api/documents/{doc_id}/evidence")
def evidence(
    doc_id: str,
    accepted: int = 1,
    kind: str = "",
    q: str = "",
    page: int = -1,
    labelled: int = -1,
    limit: int = 300,
):
    sql = (
        "SELECT e.*, c.subject, c.metric, c.period, c.basis, c.is_fact, c.model"
        " FROM evidence e LEFT JOIN claims c ON c.evidence_id = e.id"
        " WHERE e.doc_id = ? AND e.accepted = ?"
    )
    args = [doc_id, accepted]
    if kind:
        sql += " AND e.kind = ?"
        args.append(kind)
    if q:
        sql += " AND (e.text LIKE ? OR e.row_label LIKE ? OR c.metric LIKE ?)"
        args += [f"%{q}%"] * 3
    if page >= 0:
        sql += " AND e.pdf_page = ?"
        args.append(page)
    if labelled == 1:
        sql += " AND c.id IS NOT NULL"
    elif labelled == 0:
        sql += " AND c.id IS NULL"
    sql += " ORDER BY e.pdf_page, e.rowid LIMIT ?"
    args.append(limit)

    with db() as conn:
        return [dict(r) for r in conn.execute(sql, args)]
