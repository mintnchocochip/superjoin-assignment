"""Upload PDFs and inspect the evidence mined from them.

Storage is SQLite for now. The architecture commits to MongoDB, but the schema
is still moving and a daemon is friction this early; the swap is the four
queries in this file, and it happens once claim/verdict shapes settle.
"""

import hashlib
import pathlib
import sqlite3
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

import extract

ROOT = pathlib.Path(__file__).parent
UPLOADS = ROOT / "uploads"
DB_PATH = ROOT / "facts.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id           TEXT PRIMARY KEY,
    filename     TEXT NOT NULL,
    sha256       TEXT NOT NULL UNIQUE,
    page_count   INTEGER NOT NULL,
    uploaded_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id        TEXT NOT NULL,
    pdf_page      INTEGER NOT NULL,
    printed_page  TEXT,
    value         TEXT NOT NULL,
    currency      TEXT,
    unit          TEXT,
    line          TEXT NOT NULL,
    label         TEXT,
    char_start    INTEGER,
    char_end      INTEGER,
    accepted      INTEGER NOT NULL,
    reject_reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_candidates_doc ON candidates(doc_id, accepted);
"""

app = FastAPI(title="Fact Knowledge Layer")


def db():
    conn = sqlite3.connect(DB_PATH)
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

    # Regex mining over 100 pages takes about a second, so this stays synchronous.
    # It becomes a background task when the labelling model joins the pipeline.
    rows = list(extract.mine(path))

    with db() as conn:
        conn.execute(
            "INSERT INTO documents VALUES (?, ?, ?, ?, ?)",
            (
                doc_id,
                file.filename,
                digest,
                extract.page_count(path),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        conn.executemany(
            "INSERT INTO candidates (doc_id, pdf_page, printed_page, value, currency,"
            " unit, line, label, char_start, char_end, accepted, reject_reason)"
            " VALUES (:doc_id, :pdf_page, :printed_page, :value, :currency, :unit,"
            " :line, :label, :char_start, :char_end, :accepted, :reject_reason)",
            [dict(r, doc_id=doc_id) for r in rows],
        )

    return {
        "doc_id": doc_id,
        "duplicate": False,
        "mined": len(rows),
        "accepted": sum(1 for r in rows if r["accepted"]),
    }


@app.get("/api/documents")
def documents():
    with db() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT d.*,"
                "  (SELECT COUNT(*) FROM candidates c"
                "     WHERE c.doc_id = d.id AND c.accepted = 1) AS accepted,"
                "  (SELECT COUNT(*) FROM candidates c"
                "     WHERE c.doc_id = d.id AND c.accepted = 0) AS rejected"
                " FROM documents d ORDER BY d.uploaded_at DESC"
            )
        ]


@app.get("/api/documents/{doc_id}/candidates")
def candidates(doc_id: str, accepted: int = 1, q: str = "", page: int = -1, limit: int = 300):
    sql = "SELECT * FROM candidates WHERE doc_id = ? AND accepted = ?"
    args = [doc_id, accepted]
    if q:
        sql += " AND (line LIKE ? OR label LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    if page >= 0:
        sql += " AND pdf_page = ?"
        args.append(page)
    sql += " ORDER BY pdf_page, id LIMIT ?"
    args.append(limit)

    with db() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


@app.get("/api/documents/{doc_id}/rejections")
def rejections(doc_id: str):
    """Reject reasons by frequency, for tuning the mining rules against real pages."""
    with db() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT reject_reason AS reason, COUNT(*) AS n FROM candidates"
                " WHERE doc_id = ? AND accepted = 0"
                " GROUP BY reject_reason ORDER BY n DESC",
                (doc_id,),
            )
        ]


@app.delete("/api/documents/{doc_id}")
def remove(doc_id: str):
    with db() as conn:
        conn.execute("DELETE FROM candidates WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    (UPLOADS / f"{doc_id}.pdf").unlink(missing_ok=True)
    return {"deleted": doc_id}
