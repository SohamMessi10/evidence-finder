"""The library: documents, their passages, and the problems found while reading them, in SQLite.

    documents  one row per source: a PEP or an imported file, with where it came from
    passages   the searchable pieces, each pointing back to its document, section, page and lines
    problems   anything worth knowing about a document, including why it couldn't be indexed

STRICT tables and CHECK constraints keep impossible rows out (a passage with its last line
before its first, a PDF page number of 0). Re-importing a document replaces it, so importing
the same thing twice never duplicates passages.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from evfind.extract import Extracted, Passage

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,                       -- "pep-0572", or "file:<path>"
    kind TEXT NOT NULL CHECK (kind IN ('pep', 'text', 'markdown', 'rst', 'pdf')),
    title TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,                           -- the file it was read from
    meta TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(meta)),
    status TEXT NOT NULL CHECK (status IN ('indexed', 'unusable'))
) STRICT;

CREATE TABLE passages (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    text TEXT NOT NULL CHECK (length(trim(text)) > 0),
    section TEXT NOT NULL DEFAULT '',
    anchor TEXT NOT NULL DEFAULT '',
    page INTEGER CHECK (page IS NULL OR page >= 1),
    line_start INTEGER CHECK (line_start IS NULL OR line_start >= 1),
    line_end INTEGER CHECK (line_end IS NULL OR line_end >= line_start),
    UNIQUE (document_id, ordinal)
) STRICT;

CREATE TABLE problems (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    detail TEXT NOT NULL
) STRICT;
"""


class StoreError(Exception):
    """The library file can't be used."""


class Library:
    def __init__(self, conn: sqlite3.Connection, path: Path):
        self.conn, self.path = conn, path

    @classmethod
    def open(cls, path: Path, *, create: bool = False) -> Library:
        path = Path(path)
        if not create and not path.exists():
            raise StoreError(f"{path} doesn't exist; create it by importing something, e.g. evfind ingest --peps")
        try:
            conn = sqlite3.connect(path, timeout=5.0)
        except sqlite3.Error as exc:
            raise StoreError(f"{path} can't be opened ({exc})") from None
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                if conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]:
                    raise StoreError(f"{path} is a SQLite file, but not an Evidence Finder library")
                with conn:
                    conn.executescript(SCHEMA)
                    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise StoreError(f"{path} was made by a different version (schema {version}, expected {SCHEMA_VERSION})")
        except sqlite3.DatabaseError as exc:
            conn.close()
            raise StoreError(f"{path} isn't a usable library ({exc})") from None
        except StoreError:
            conn.close()
            raise
        return cls(conn, path)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Library:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def put(self, key: str, kind: str, title: str, source: str, extracted: Extracted, *,
            url: str = "", meta: dict | None = None) -> int:
        """Store one document and its passages, replacing any earlier copy, in one transaction."""
        status = "unusable" if extracted.unusable else "indexed"
        with self.conn:
            self.conn.execute("DELETE FROM documents WHERE key = ?", (key,))
            doc_id = self.conn.execute(
                "INSERT INTO documents (key, kind, title, url, source, meta, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key, kind, title, url, source, json.dumps(meta or {}), status)).lastrowid
            self.conn.executemany(
                "INSERT INTO passages (document_id, ordinal, text, section, anchor, page, line_start, line_end) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(doc_id, p.ordinal, p.text, p.section, p.anchor, p.page, p.line_start, p.line_end)
                 for p in extracted.passages])
            problems = ([extracted.unusable] if extracted.unusable else []) + extracted.problems
            self.conn.executemany("INSERT INTO problems (document_id, detail) VALUES (?, ?)",
                                  [(doc_id, p) for p in problems])
        return doc_id

    def passages(self, key: str) -> list[Passage]:
        rows = self.conn.execute(
            "SELECT p.ordinal, p.text, p.section, p.anchor, p.page, p.line_start, p.line_end FROM passages p "
            "JOIN documents d ON d.id = p.document_id WHERE d.key = ? ORDER BY p.ordinal", (key,)).fetchall()
        return [Passage(*row) for row in rows]

    def document(self, key: str) -> dict | None:
        row = self.conn.execute("SELECT key, kind, title, url, source, meta, status FROM documents WHERE key = ?",
                                (key,)).fetchone()
        if row is None:
            return None
        doc = dict(zip(("key", "kind", "title", "url", "source", "meta", "status"), row))
        doc["meta"] = json.loads(doc["meta"])
        doc["problems"] = [r[0] for r in self.conn.execute(
            "SELECT detail FROM problems p JOIN documents d ON d.id = p.document_id WHERE d.key = ? ORDER BY p.id",
            (key,))]
        return doc

    def stats(self) -> dict:
        one = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "documents": one("SELECT count(*) FROM documents"),
            "indexed": one("SELECT count(*) FROM documents WHERE status = 'indexed'"),
            "unusable": one("SELECT count(*) FROM documents WHERE status = 'unusable'"),
            "passages": one("SELECT count(*) FROM passages"),
            "words": sum(len(text.split()) for (text,) in self.conn.execute("SELECT text FROM passages")),
            "with_problems": one("SELECT count(DISTINCT document_id) FROM problems"),
        }
