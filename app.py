"""HTTP layer: upload PDFs, read back facts and relations. Run with

    uvicorn app:app --reload
"""

import pathlib
import shutil
import threading

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

import db
import llm
import pipeline

UPLOADS = pathlib.Path("uploads")
UPLOADS.mkdir(exist_ok=True)
STATIC = pathlib.Path(__file__).parent / "static"

app = FastAPI(title="Fact Knowledge Layer")
db.init()

# ponytail: one document at a time. The model calls inside a document are already
# parallel; a queue only becomes worth it with several people uploading at once.
_one_at_a_time = threading.Lock()


@app.get("/")
def home():
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
def config():
    return {"backend": llm.BACKEND, "model": llm.MODEL}


@app.post("/api/upload")
async def upload(background: BackgroundTasks, files: list[UploadFile],
                 max_pages: int = Form(pipeline.MAX_PAGES)):
    queued = []
    for upload_file in files:
        name = pathlib.Path(upload_file.filename or "document.pdf").name
        if not name.lower().endswith(".pdf"):
            raise HTTPException(400, f"{name} is not a PDF")
        target = UPLOADS / name
        with target.open("wb") as out:
            shutil.copyfileobj(upload_file.file, out)
        doc_id = db.write("INSERT INTO documents (name, path) VALUES (?,?)",
                          (name, str(target)))
        background.add_task(_run, doc_id, max_pages)
        queued.append({"id": doc_id, "name": name})
    return {"queued": queued}


def _run(doc_id, max_pages):
    with _one_at_a_time:
        try:
            pipeline.process(doc_id, max_pages)
        except Exception:
            pass  # the failure and its reason are already on the document row


@app.get("/api/documents")
def documents():
    return db.rows("""
        SELECT d.*,
               (SELECT COUNT(*) FROM facts f WHERE f.doc_id=d.id AND f.grounded=1) AS facts,
               (SELECT COUNT(*) FROM facts f WHERE f.doc_id=d.id AND f.grounded=0) AS dropped
        FROM documents d ORDER BY d.id DESC""")


@app.get("/api/facts")
def facts(doc: int = 0, grounded: int = 1, q: str = ""):
    sql = ["SELECT f.*, d.name AS doc FROM facts f JOIN documents d ON d.id=f.doc_id",
           "WHERE f.grounded=?"]
    params = [grounded]
    if doc:
        sql.append("AND f.doc_id=?")
        params.append(doc)
    if q.strip():
        sql.append("AND (f.subject LIKE ? OR f.attribute LIKE ? OR f.value LIKE ? OR f.quote LIKE ?)")
        params += [f"%{q.strip()}%"] * 4
    sql.append("ORDER BY f.doc_id DESC, f.page, f.id LIMIT 2000")
    return db.rows(" ".join(sql), tuple(params))


@app.get("/api/relations")
def relations(relation: str = ""):
    sql = ["""SELECT r.*,
                     a.subject a_subject, a.attribute a_attribute, a.value a_value,
                     a.unit a_unit, a.period a_period, a.scope a_scope,
                     a.quote a_quote, a.page a_page, a.doc_id a_doc_id, da.name a_doc,
                     b.subject b_subject, b.attribute b_attribute, b.value b_value,
                     b.unit b_unit, b.period b_period, b.scope b_scope,
                     b.quote b_quote, b.page b_page, b.doc_id b_doc_id, db_.name b_doc
              FROM relations r
              JOIN facts a ON a.id=r.a_id  JOIN documents da  ON da.id=a.doc_id
              JOIN facts b ON b.id=r.b_id  JOIN documents db_ ON db_.id=b.doc_id"""]
    params = ()
    if relation:
        sql.append("WHERE r.relation=?")
        params = (relation,)
    sql.append("ORDER BY r.confidence DESC, r.id DESC LIMIT 500")
    return db.rows(" ".join(sql), params)


@app.get("/pdf/{doc_id}")
def pdf(doc_id: int):
    found = db.rows("SELECT path, name FROM documents WHERE id=?", (doc_id,))
    if not found:
        raise HTTPException(404, "no such document")
    return FileResponse(found[0]["path"], media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{found[0]["name"]}"'})
