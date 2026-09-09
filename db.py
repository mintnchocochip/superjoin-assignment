"""SQLite. Three tables: documents, facts, relations."""

import os
import sqlite3

PATH = os.getenv("FACTS_DB", "facts.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL,
    path     TEXT NOT NULL,
    pages    INTEGER DEFAULT 0,
    status   TEXT DEFAULT 'queued',   -- queued | extracting | linking | done | failed
    note     TEXT DEFAULT '',
    added_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS facts (
    id        INTEGER PRIMARY KEY,
    doc_id    INTEGER NOT NULL REFERENCES documents(id),
    subject   TEXT, attribute TEXT, value TEXT,
    unit      TEXT, period    TEXT, scope TEXT,
    quote     TEXT, page INTEGER,
    grounded  INTEGER DEFAULT 1,       -- 0 = quote not found in the page, kept for inspection
    note      TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS relations (
    id         INTEGER PRIMARY KEY,
    a_id       INTEGER NOT NULL REFERENCES facts(id),
    b_id       INTEGER NOT NULL REFERENCES facts(id),
    relation   TEXT,                   -- corroborates | contradicts | reconcilable
    confidence REAL,
    reasoning  TEXT,
    UNIQUE (a_id, b_id)
);

CREATE INDEX IF NOT EXISTS facts_doc ON facts(doc_id);
"""


def connect():
    """A fresh connection per call - worker threads must not share one."""
    conn = sqlite3.connect(PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init():
    with connect() as conn:
        conn.executescript(SCHEMA)


def rows(sql, params=()):
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def write(sql, params=()):
    with connect() as conn:
        return conn.execute(sql, params).lastrowid
