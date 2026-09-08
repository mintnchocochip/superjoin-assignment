"""MongoDB storage for the fact knowledge layer.

Four collections, matching the schema documented in the README:

  pdfs         one per uploaded document
  evidence     mined spans - written once at ingest, by regex, never by a model
  claims       what a piece of evidence is about, carrying its group_key
  claim_groups one per (subject, metric), holding the pairwise verdicts

The README draws evidence embedded on the claim. It is a collection here
instead, because the two halves turned out to have different writers and
different lifecycles: evidence is deterministic and immutable, claims are model
written and re-runnable. A claim references its evidence by id, so re-labelling
never touches a stored quote. Everything else follows the README.

group_key is `subject|metric`, deliberately loose: period, basis and vintage
stay off the key so that two figures differing only by context still land in
the same group and can be reconciled instead of reported as a contradiction.
"""

import os
import re

from pymongo import ASCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, OperationFailure

URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/factlayer")
DB_NAME = os.environ.get("MONGO_DB", "factlayer")

# Write concern: a claim that only reached the primary is a claim that vanishes
# when the primary does. Majority is the whole reason for running three nodes.
WRITE_CONCERN = {"w": "majority", "j": True}

SLUG = re.compile(r"[^a-z0-9]+")

_client = None


def slug(text):
    return SLUG.sub("-", (text or "").strip().lower()).strip("-")


def group_key(subject, metric):
    """`subject|metric`. Qualifiers stay out of it, on purpose."""
    if not subject or not metric:
        return None
    return f"{slug(subject)}|{slug(metric)}"


def client():
    global _client
    if _client is None:
        _client = MongoClient(URI, tz_aware=True, serverSelectionTimeoutMS=5000)
    return _client


def db():
    return client().get_database(DB_NAME)


def connect():
    """Open the connection and prove it works. Returns the topology description."""
    info = client().admin.command("hello")
    return {
        "primary": info.get("primary"),
        "replica_set": info.get("setName"),
        "members": info.get("hosts", []),
        "read_only": not info.get("isWritablePrimary", False),
    }


def ensure_indexes():
    """Indexes the design depends on, not decoration.

    - pdfs.sha256 unique: a re-uploaded file would otherwise manufacture
      corroborations between a document and its own copy.
    - claims.evidence_id unique: one claim per piece of evidence, so re-labelling
      replaces rather than accumulates.
    - claims.group_key: this index IS the blocking step. A new claim compares
      against its group only, which is what keeps adjudication off O(n^2).
    """
    d = db()
    d.pdfs.create_index([("sha256", ASCENDING)], unique=True, name="ux_sha256")
    d.evidence.create_index(
        [("doc_id", ASCENDING), ("accepted", ASCENDING), ("kind", ASCENDING)],
        name="ix_doc_accepted_kind",
    )
    d.evidence.create_index([("pdf_page", ASCENDING)], name="ix_page")
    d.claims.create_index([("evidence_id", ASCENDING)], unique=True, name="ux_evidence")
    d.claims.create_index([("group_key", ASCENDING)], name="ix_group_key")
    d.claims.create_index([("doc_id", ASCENDING), ("is_fact", ASCENDING)], name="ix_doc_fact")
    d.claim_groups.create_index([("subject", ASCENDING), ("metric", ASCENDING)], name="ix_subject_metric")


# --- documents -------------------------------------------------------------

def find_pdf_by_hash(digest):
    return db().pdfs.find_one({"sha256": digest})


def insert_pdf(doc):
    try:
        db().pdfs.with_options(write_concern=_wc()).insert_one(doc)
        return True
    except DuplicateKeyError:
        return False


def list_pdfs():
    """Documents with their evidence and claim counts, newest first."""
    return list(
        db().pdfs.aggregate(
            [
                {"$sort": {"uploaded_at": -1}},
                {
                    "$lookup": {
                        "from": "evidence",
                        "let": {"doc": "$_id"},
                        "pipeline": [
                            {"$match": {"$expr": {"$eq": ["$doc_id", "$$doc"]}}},
                            {"$group": {
                                "_id": None,
                                "accepted": {"$sum": {"$cond": ["$accepted", 1, 0]}},
                                "rejected": {"$sum": {"$cond": ["$accepted", 0, 1]}},
                            }},
                        ],
                        "as": "ev",
                    }
                },
                {
                    "$lookup": {
                        "from": "claims",
                        "let": {"doc": "$_id"},
                        "pipeline": [
                            {"$match": {"$expr": {"$eq": ["$doc_id", "$$doc"]}}},
                            {"$group": {
                                "_id": None,
                                "labelled": {"$sum": 1},
                                "facts": {"$sum": {"$cond": ["$is_fact", 1, 0]}},
                            }},
                        ],
                        "as": "cl",
                    }
                },
                {
                    "$project": {
                        "id": "$_id", "_id": 0, "filename": 1, "subject": 1,
                        "page_count": 1, "uploaded_at": 1,
                        "accepted": {"$ifNull": [{"$first": "$ev.accepted"}, 0]},
                        "rejected": {"$ifNull": [{"$first": "$ev.rejected"}, 0]},
                        "labelled": {"$ifNull": [{"$first": "$cl.labelled"}, 0]},
                        "facts": {"$ifNull": [{"$first": "$cl.facts"}, 0]},
                    }
                },
            ]
        )
    )


# --- evidence --------------------------------------------------------------

def insert_evidence(rows):
    """Insert mined evidence, ignoring rows already present.

    Ids are content-derived, so re-mining a document is a no-op rather than a
    duplication. ordered=False lets the rest of the batch land when some ids
    already exist.
    """
    if not rows:
        return 0
    docs = [dict(r, _id=r["id"]) for r in rows]
    for d in docs:
        d.pop("id", None)
    try:
        result = db().evidence.with_options(write_concern=_wc()).insert_many(docs, ordered=False)
        return len(result.inserted_ids)
    except OperationFailure as exc:
        # Duplicate ids are expected on a re-mine; anything else is real.
        if getattr(exc, "code", None) not in (11000,) and "E11000" not in str(exc):
            raise
        return 0


def pending_evidence(doc_id):
    """Accepted evidence for a document that has no claim yet."""
    return list(
        db().evidence.aggregate(
            [
                {"$match": {"doc_id": doc_id, "accepted": True}},
                {"$lookup": {
                    "from": "claims", "localField": "_id",
                    "foreignField": "evidence_id", "as": "claim",
                }},
                {"$match": {"claim": {"$size": 0}}},
                {"$lookup": {
                    "from": "pdfs", "localField": "doc_id",
                    "foreignField": "_id", "as": "pdf",
                }},
                {"$sort": {"pdf_page": 1}},
                {"$addFields": {"id": "$_id", "subject": {"$first": "$pdf.subject"}}},
                {"$project": {"claim": 0, "pdf": 0}},
            ]
        )
    )


def find_evidence(doc_id, accepted=True, kind="", q="", page=-1, labelled=-1, limit=300):
    """Evidence joined to its claim, for the inspection UI."""
    match = {"doc_id": doc_id, "accepted": bool(accepted)}
    if kind:
        match["kind"] = kind
    if page >= 0:
        match["pdf_page"] = page

    pipeline = [
        {"$match": match},
        {"$lookup": {
            "from": "claims", "localField": "_id",
            "foreignField": "evidence_id", "as": "claim",
        }},
        {"$addFields": {"claim": {"$first": "$claim"}}},
    ]
    if labelled == 1:
        pipeline.append({"$match": {"claim": {"$ne": None}}})
    elif labelled == 0:
        pipeline.append({"$match": {"claim": None}})
    if q:
        rx = {"$regex": re.escape(q), "$options": "i"}
        pipeline.append({"$match": {"$or": [
            {"text": rx}, {"row_label": rx}, {"claim.metric": rx},
        ]}})

    pipeline += [
        {"$sort": {"pdf_page": 1}},
        {"$limit": int(limit)},
        {"$project": {
            "id": "$_id", "_id": 0,
            "kind": 1, "pdf_page": 1, "printed_page": 1, "text": 1, "row_label": 1,
            "section": 1, "col_header": 1, "value": 1, "currency": 1, "unit": 1,
            "char_start": 1, "char_end": 1, "reject_reason": 1,
            "subject": "$claim.subject", "metric": "$claim.metric",
            "metric_evidence": "$claim.metric_evidence", "period": "$claim.period",
            "basis": "$claim.basis", "basis_evidence": "$claim.basis_evidence",
            "vintage": "$claim.vintage", "scope": "$claim.scope",
            "confidence": "$claim.confidence", "is_fact": "$claim.is_fact",
            "model": "$claim.model", "group_key": "$claim.group_key",
        }},
    ]
    return list(db().evidence.aggregate(pipeline))


# --- claims and groups -----------------------------------------------------

def upsert_claims(claims):
    """Write model output. One claim per evidence id, replaced on re-label."""
    if not claims:
        return 0
    coll = db().claims.with_options(write_concern=_wc())
    written = 0
    for claim in claims:
        doc = dict(claim)
        doc["group_key"] = group_key(doc.get("subject"), doc.get("metric"))
        coll.replace_one({"evidence_id": doc["evidence_id"]}, doc, upsert=True)
        written += 1
    return written


def sync_groups(doc_id=None):
    """Materialise claim_groups from the claims that carry a group_key.

    Verdicts are not computed here - that is the adjudication step. This keeps
    the group documents in existence with their members, so blocking has
    something to block against.
    """
    match = {"group_key": {"$ne": None}, "is_fact": True}
    if doc_id:
        match["doc_id"] = doc_id
    groups = db().claims.aggregate([
        {"$match": match},
        {"$group": {
            "_id": "$group_key",
            "subject": {"$first": "$subject"},
            "metric": {"$first": "$metric"},
            "members": {"$addToSet": "$_id"},
            "docs": {"$addToSet": "$doc_id"},
        }},
    ])
    coll = db().claim_groups.with_options(write_concern=_wc())
    count = 0
    for g in groups:
        coll.update_one(
            {"_id": g["_id"]},
            {"$set": {
                "subject": g["subject"], "metric": g["metric"],
                "member_count": len(g["members"]), "doc_count": len(g["docs"]),
            },
             "$setOnInsert": {"verdicts": []}},
            upsert=True,
        )
        count += 1
    return count


def cross_document_groups(limit=100):
    """Groups whose claims come from more than one document.

    These are the only groups that can produce a finding, so this is what the
    adjudication step will read.
    """
    return list(
        db().claim_groups.find({"doc_count": {"$gt": 1}})
        .sort("member_count", -1)
        .limit(int(limit))
    )


def _wc():
    from pymongo import WriteConcern
    return WriteConcern(**WRITE_CONCERN)
