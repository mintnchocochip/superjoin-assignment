"""MongoDB storage for the fact knowledge layer.

Five collections, matching the schema documented in the README:

  corpora      one per entity - the layer that makes documents comparable
  pdfs         one per uploaded document, assigned to a corpus
  evidence     mined spans - written once at ingest, by regex, never by a model
  claims       what a piece of evidence is about, carrying its group_key
  claim_groups one per (corpus, metric), holding the pairwise verdicts

The README draws evidence embedded on the claim. It is a collection here
instead, because the two halves turned out to have different writers and
different lifecycles: evidence is deterministic and immutable, claims are model
written and re-runnable. A claim references its evidence by id, so re-labelling
never touches a stored quote. Everything else follows the README.

group_key is `corpus|metric`, deliberately loose: period, basis, vintage and
doc_type all stay off the key so that two figures differing only by context
still land in the same group and can be reconciled instead of reported as a
contradiction. The key is derived from the corpus rather than stored per
document, so correcting a misfiled document is a bulk update - never a
re-labelling run.
"""

import os
import re
from datetime import datetime, timezone

from pymongo import ASCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

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
    """`subject|metric`. Qualifiers stay out of it, on purpose.

    `subject` here is a corpus id, not anything derived from a filename. Two
    documents about the same entity must produce the same key or no group can
    ever span more than one document.
    """
    if not subject or not metric:
        return None
    return f"{slug(subject)}|{slug(metric)}"


# --- corpora ---------------------------------------------------------------
# A corpus is the entity a set of documents is about. It exists so that grouping
# happens before evidence is labelled: the annual report and the earnings deck
# are recognised as being about one company first, and only then do their claims
# get a key that lets them be compared.

def list_corpora():
    """Corpora with their document counts."""
    return list(db().corpora.aggregate([
        {"$lookup": {"from": "pdfs", "localField": "_id",
                     "foreignField": "corpus_id", "as": "docs"}},
        {"$project": {"id": "$_id", "_id": 0, "name": 1, "aliases": 1,
                      "doc_count": {"$size": "$docs"}}},
        {"$sort": {"name": 1}},
    ]))


def find_corpus(name, cutoff=0.86):
    """An existing corpus matching this name, or None.

    Exact slug first, then the alias list, then fuzzy - the same escalation
    `label.target_metric` uses, and for the same reason: "Delhivery Limited" and
    "Delhivery Ltd" are one company, and if they are not recognised as one the
    system can never compare their documents.
    """
    if not name:
        return None
    key = slug(name)
    if hit := db().corpora.find_one({"_id": key}):
        return hit
    if hit := db().corpora.find_one({"aliases": key}):
        return hit

    import difflib
    known = {c["_id"]: c for c in db().corpora.find({}, {"name": 1, "aliases": 1})}
    close = difflib.get_close_matches(key, list(known), n=1, cutoff=cutoff)
    return known[close[0]] if close else None


def find_or_create_corpus(name):
    """Resolve a proposed entity name to a corpus, creating one if it is new."""
    if not name:
        return None
    if existing := find_corpus(name):
        if slug(name) != existing["_id"]:
            db().corpora.update_one({"_id": existing["_id"]},
                                    {"$addToSet": {"aliases": slug(name)}})
        return existing
    doc = {"_id": slug(name), "name": name.strip(), "aliases": [],
           "created_at": datetime.now(timezone.utc)}
    db().corpora.with_options(write_concern=_wc()).insert_one(doc)
    return doc


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
    d.claim_groups.create_index([("corpus_id", ASCENDING), ("metric", ASCENDING)],
                                name="ix_corpus_metric")
    d.corpora.create_index([("name", ASCENDING)], name="ix_name")
    d.corpora.create_index([("aliases", ASCENDING)], name="ix_aliases")
    d.pdfs.create_index([("corpus_id", ASCENDING)], name="ix_corpus")
    # Same metric and document type across different corpora: reading one
    # company against another. No verdict logic uses this yet; the index is here
    # so adding that later is a query rather than a migration.
    d.claims.create_index(
        [("metric", ASCENDING), ("doc_type", ASCENDING), ("corpus_id", ASCENDING)],
        name="ix_peer_comparison",
    )


# --- documents -------------------------------------------------------------

def find_pdf_by_hash(digest):
    return db().pdfs.find_one({"sha256": digest})


def get_pdf(doc_id):
    return db().pdfs.find_one({"_id": doc_id})


def set_doc(doc_id, **fields):
    """Update a document's row. Used to publish progress while work is running.

    Ingestion and labelling both take long enough that a UI showing nothing is
    indistinguishable from a UI showing a hang, so each stage writes where it has
    got to and the page reads it back.
    """
    fields["updated_at"] = datetime.now(timezone.utc)
    db().pdfs.update_one({"_id": doc_id}, {"$set": fields})


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
                {"$lookup": {"from": "corpora", "localField": "corpus_id",
                             "foreignField": "_id", "as": "corpus"}},
                {
                    "$project": {
                        "id": "$_id", "_id": 0, "filename": 1, "subject": 1,
                        "page_count": 1, "uploaded_at": 1,
                        "corpus_id": 1, "corpus_confirmed": 1, "doc_type": 1,
                        "proposed_subject": 1, "proposed_evidence": 1,
                        "state": 1, "progress": 1, "error": 1,
                        "corpus_name": {"$first": "$corpus.name"},
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
    """Write mined evidence, replacing any row already present.

    Ids are content-derived, so re-mining a document produces the same ids and
    this reconciles rather than duplicating. It replaces rather than ignores
    because a miner improvement - a new column band, a better label - has to be
    able to reach documents that were ingested before it existed. Ignoring
    duplicates made re-mining a silent no-op, which is the worst of both.

    Claims are untouched: they reference evidence by id, and the ids do not move.
    """
    if not rows:
        return 0
    from pymongo import ReplaceOne

    ops = []
    for row in rows:
        doc = dict(row, _id=row["id"])
        doc.pop("id", None)
        ops.append(ReplaceOne({"_id": doc["_id"]}, doc, upsert=True))

    result = db().evidence.with_options(write_concern=_wc()).bulk_write(ops, ordered=False)
    return result.upserted_count + result.modified_count


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
                {"$lookup": {
                    "from": "corpora", "localField": "pdf.corpus_id",
                    "foreignField": "_id", "as": "corpus",
                }},
                {"$sort": {"pdf_page": 1}},
                # The subject shown to the prefilter and the labeller is the
                # corpus name, never anything derived from the filename.
                {"$addFields": {"id": "$_id", "subject": {"$first": "$corpus.name"},
                                "doc_type": {"$first": "$pdf.doc_type"}}},
                {"$project": {"claim": 0, "pdf": 0, "corpus": 0}},
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
    """Write model output. One claim per evidence id, replaced on re-label.

    The key comes from corpus_id, never from anything the model returned as a
    subject and never from a filename. That is what lets two documents about one
    entity produce one group.
    """
    if not claims:
        return 0
    coll = db().claims.with_options(write_concern=_wc())
    written = 0
    for claim in claims:
        doc = dict(claim)
        # The bucket, not the wording. A group keyed on what the model happened to
        # call the row put "revenue from operations", "total income" and "revenue"
        # in three groups of one, and a group of one corroborates nothing.
        doc["group_key"] = group_key(doc.get("corpus_id"),
                                     doc.get("metric_key") or doc.get("metric"))
        coll.replace_one({"evidence_id": doc["evidence_id"]}, doc, upsert=True)
        written += 1
    return written


def set_corpus(doc_id, corpus_id, doc_type=None, confirmed=True):
    """Assign a document to a corpus and re-key its claims.

    Deliberately cheap. Labelling a document costs tens of minutes; deciding
    which company it is about is a judgement that gets revised. Storing the key
    as a derived field means correcting a misfiled document is two bulk updates
    and a group sync, with no model call anywhere in the path.
    """
    fields = {"corpus_id": corpus_id, "corpus_confirmed": bool(confirmed)}
    if doc_type is not None:
        fields["doc_type"] = doc_type
    db().pdfs.with_options(write_concern=_wc()).update_one(
        {"_id": doc_id}, {"$set": fields})

    claims = db().claims.with_options(write_concern=_wc())
    claims.update_many({"doc_id": doc_id}, {"$set": {"corpus_id": corpus_id}})
    if doc_type is not None:
        claims.update_many({"doc_id": doc_id}, {"$set": {"doc_type": doc_type}})

    # Re-key only what has a metric; a claim with none never had a key.
    for claim in claims.find(
        {"doc_id": doc_id,
         "$or": [{"metric_key": {"$nin": [None, ""]}}, {"metric": {"$nin": [None, ""]}}]},
        {"metric": 1, "metric_key": 1},
    ):
        claims.update_one(
            {"_id": claim["_id"]},
            {"$set": {"group_key": group_key(
                corpus_id, claim.get("metric_key") or claim.get("metric"))}})

    sync_groups()
    return db().claims.count_documents({"doc_id": doc_id})


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
            "corpus_id": {"$first": "$corpus_id"},
            "subject": {"$first": "$subject"},
            # The group is named by its bucket, which every member shares by
            # construction. A member's own wording stays on the member.
            "metric": {"$first": {"$ifNull": ["$metric_key", "$metric"]}},
            "members": {"$addToSet": "$_id"},
            "docs": {"$addToSet": "$doc_id"},
            # Recorded so a future peer query can ask for one document type
            # across corpora without touching this schema again.
            "doc_types": {"$addToSet": "$doc_type"},
        }},
    ])
    coll = db().claim_groups.with_options(write_concern=_wc())
    count = 0
    for g in groups:
        coll.update_one(
            {"_id": g["_id"]},
            {"$set": {
                "corpus_id": g["corpus_id"], "subject": g["subject"],
                "metric": g["metric"],
                "doc_types": [t for t in g["doc_types"] if t],
                "member_count": len(g["members"]), "doc_count": len(g["docs"]),
            },
             "$setOnInsert": {"verdicts": []}},
            upsert=True,
        )
        count += 1
    return count


def group_members(group_key):
    """Claims in a group, each carrying the value from its own evidence.

    The value is joined rather than copied onto the claim. It lives in exactly
    one place - the immutable evidence row - so adjudication reads the figure
    the document actually printed, not a transcription of it.
    """
    rows = db().claims.aggregate([
        {"$match": {"group_key": group_key, "is_fact": True}},
        {"$lookup": {"from": "evidence", "localField": "evidence_id",
                     "foreignField": "_id", "as": "ev"}},
        {"$addFields": {"ev": {"$first": "$ev"}}},
        {"$match": {"ev": {"$ne": None}}},
        {"$addFields": {
            "value": "$ev.value", "currency": "$ev.currency",
            "raw_unit": "$ev.unit", "pdf_page": "$ev.pdf_page",
            "printed_page": "$ev.printed_page", "quote": "$ev.text",
        }},
        {"$project": {"ev": 0}},
    ])
    # The labeller's unit is an enum it may have left blank; the miner's is
    # whatever the page printed. Prefer whichever is actually populated.
    return [dict(r, unit=(r.get("unit") or r.get("raw_unit") or "")) for r in rows]


def adjudicate_groups(limit=500):
    """Compute verdicts for every group with more than one member.

    Deterministic and cheap - no model call anywhere in this path - so it is
    safe to re-run after every labelling batch.
    """
    import verdict as verdicts

    coll = db().claim_groups.with_options(write_concern=_wc())
    done = 0
    for group in db().claim_groups.find({"member_count": {"$gt": 1}}).limit(int(limit)):
        members = group_members(group["_id"])
        if len(members) < 2:
            continue
        pairs = verdicts.adjudicate(members)
        coll.update_one({"_id": group["_id"]}, {"$set": {
            "verdicts": pairs,
            "counts": {t: sum(1 for p in pairs if p["type"] == t)
                       for t in ("corroborates", "contradicts", "reconcilable",
                                 "not_comparable")},
        }})
        done += 1
    return done


def findings(kind="", cross_document_only=False, limit=100):
    """Adjudicated pairs worth showing, newest-largest groups first.

    `not_comparable` is stored but never returned unless asked for by name: it
    is the bucket that keeps noise out of the demo, not a finding.
    """
    match = {"verdicts": {"$ne": []}}
    pipeline = [
        {"$match": match},
        {"$unwind": "$verdicts"},
        {"$match": {"verdicts.type": kind} if kind
                   else {"verdicts.type": {"$ne": "not_comparable"}}},
    ]
    if cross_document_only:
        pipeline.append({"$match": {"verdicts.cross_document": True}})
    pipeline += [
        {"$sort": {"verdicts.cross_document": -1, "verdicts.confidence": -1}},
        {"$limit": int(limit)},
        {"$project": {"group_key": "$_id", "_id": 0, "subject": 1, "metric": 1,
                      "doc_types": 1, "verdict": "$verdicts"}},
    ]
    return list(db().claim_groups.aggregate(pipeline))


def claims_by_id(ids):
    """Claims with their evidence, keyed by id - for rendering a finding's two sides."""
    rows = db().claims.aggregate([
        {"$match": {"_id": {"$in": list(ids)}}},
        {"$lookup": {"from": "evidence", "localField": "evidence_id",
                     "foreignField": "_id", "as": "ev"}},
        {"$addFields": {"ev": {"$first": "$ev"}}},
        {"$lookup": {"from": "pdfs", "localField": "doc_id",
                     "foreignField": "_id", "as": "pdf"}},
        {"$project": {
            "metric": 1, "subject": 1, "period": 1, "basis": 1, "vintage": 1,
            "scope": 1, "doc_type": 1, "doc_id": 1,
            "value": "$ev.value", "currency": "$ev.currency", "unit": "$ev.unit",
            "quote": "$ev.text", "pdf_page": "$ev.pdf_page",
            "printed_page": "$ev.printed_page",
            "filename": {"$first": "$pdf.filename"},
        }},
    ])
    return {r["_id"]: r for r in rows}


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
