"""Keyword search: the library's passages, ranked by BM25 with SQLite FTS5.

A query is split into words and each word is quoted, so nothing typed can be read as FTS5
query syntax ("what is := ?" would otherwise fail with "no such column: is"). The words are
joined with OR and BM25 ranks the passages, so a passage needn't contain every word.

Results are passages, at most `per_document` from each document by default, each with the
exact place it came from and an excerpt with the matched words marked.

Ranking documents and choosing their passages are separate. FTS5's bm25() normalises by the
length of the whole row, so with a heavy title weight the best-scoring row of a document is
simply its shortest passage (a one-line copyright notice). Documents are therefore ranked by
their best row under DEFAULT_WEIGHTS, and the passage shown for each is the one whose section
and text match best (PASSAGE_WEIGHTS: the title, the same for all of them, is left out).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass

from evfind.store import Library

MARK_START, MARK_END = "\x02", "\x03"   # around matched words in an excerpt; never in passage text
QUERY_WORD = re.compile(r"[^\W_]+")      # letters and digits: what the FTS5 tokenizer keeps
MAX_QUERY_WORDS = 32
MAX_LIMIT = 100
EXCERPT_TOKENS = 32                      # FTS5 allows at most 64
# BM25 weights for the title, section and text columns, chosen on the 49 tuning questions only
# (docs/evaluation.md): title 8 had the best MRR, 0.797 against 0.721 for equal weights, and
# larger title weights or any extra section weight did no better.
DEFAULT_WEIGHTS = (8.0, 1.0, 1.0)
EQUAL_WEIGHTS = (1.0, 1.0, 1.0)  # the milestone 2 setting, kept as the baseline
PASSAGE_WEIGHTS = (0.0, 1.0, 1.0)  # choosing a document's passage: its section and text only


class SearchError(ValueError):
    """The search can't be run as asked."""


@dataclass(frozen=True)
class Hit:
    rank: int                  # from 1
    score: float               # BM25: higher is better
    key: str                   # the document: "pep-0572", or "file:<path>"
    kind: str
    title: str
    source: str                # the file it was read from
    url: str                   # the document's page, at the section when its anchor is known
    source_url: str            # the exact lines at the snapshot's commit (PEPs only)
    section: str
    page: int | None
    line_start: int | None
    line_end: int | None
    excerpt: str               # with MARK_START and MARK_END around matched words
    more_in_document: int      # how many other passages of this document also matched

    @property
    def place(self) -> str:
        """Where in the document: "Abstract · lines 17–28", "page 3"."""
        parts = [self.section] if self.section else []
        if self.page is not None:
            parts.append(f"page {self.page}")
        if self.line_start is not None:
            parts.append(f"line {self.line_start}" if self.line_start == self.line_end
                         else f"lines {self.line_start}–{self.line_end}")
        return " · ".join(parts)


@dataclass(frozen=True)
class Results:
    query: str
    words: tuple[str, ...]
    hits: list[Hit]
    passages_matched: int
    documents_matched: int


def match_expression(query: str) -> tuple[str, tuple[str, ...]]:
    """The FTS5 expression for a query: each distinct word quoted, joined with OR."""
    words: list[str] = []
    for word in QUERY_WORD.findall(query.lower()):
        if word not in words:
            words.append(word)
    if not words:
        raise SearchError("nothing to search for: the query has no letters or digits")
    words = words[:MAX_QUERY_WORDS]
    return " OR ".join(f'"{word}"' for word in words), tuple(words)


def render_excerpt(excerpt: str, start: str, end: str) -> str:
    """The excerpt on one line, with the matched words between `start` and `end`."""
    return " ".join(excerpt.split()).replace(MARK_START, start).replace(MARK_END, end)


def source_url(meta: dict, line_start: int | None, line_end: int | None) -> str:
    """For a PEP passage: its lines in the PEP repository at the snapshot's commit.

    `?plain=1` shows the file's source rather than its rendering, so the line anchor works.
    """
    commit, path = meta.get("snapshot_commit"), meta.get("path_in_repo")
    if not (commit and path and line_start):
        return ""
    lines = f"L{line_start}" if line_start == line_end else f"L{line_start}-L{line_end}"
    return f"https://github.com/python/peps/blob/{commit}/{path}?plain=1#{lines}"


def search(library: Library, query: str, *, limit: int = 10, per_document: int | None = 1,
           weights: tuple[float, float, float] = DEFAULT_WEIGHTS) -> Results:
    """The best passages for a query; at most `per_document` from each document (None: no limit)."""
    if not 1 <= limit <= MAX_LIMIT:
        raise SearchError(f"the number of results must be between 1 and {MAX_LIMIT}")
    if per_document is not None and per_document < 1:
        raise SearchError("passages per document must be at least 1")
    if len(weights) != 3 or any(not w > 0 for w in weights):
        raise SearchError("the three column weights must be positive")
    expression, words = match_expression(query)
    conn = library.conn

    # FTS5's bm25() is lower for better matches, so ascending order puts the best first; the
    # passage id breaks ties so the order never depends on the query plan.
    matched = conn.execute(
        "SELECT passage_index.rowid, p.document_id, bm25(passage_index, ?, ?, ?) AS score, "
        "bm25(passage_index, ?, ?, ?) AS fit "
        "FROM passage_index JOIN passages p ON p.id = passage_index.rowid "
        "WHERE passage_index MATCH ? ORDER BY score, passage_index.rowid",
        (*map(float, weights), *PASSAGE_WEIGHTS, expression)).fetchall()
    per_doc = Counter(document for _, document, _, _ in matched)

    chosen = []  # (passage id, document, the score its rank is shown with)
    if per_document is None:
        chosen = [(passage_id, document, score) for passage_id, document, score, _ in matched[:limit]]
    else:
        by_document: dict[int, list] = {}
        for row in matched:  # documents in order of their best row; dicts keep insertion order
            by_document.setdefault(row[1], []).append(row)
        for document, rows in by_document.items():
            best = rows[0][2]
            for passage_id, _, score, fit in sorted(rows, key=lambda r: (r[3], r[2], r[0]))[:per_document]:
                chosen.append((passage_id, document, best))
                if len(chosen) == limit:
                    break
            if len(chosen) == limit:
                break

    ids = [passage_id for passage_id, _, _ in chosen]
    marks = ",".join("?" * len(ids))
    excerpts = dict(conn.execute(
        f"SELECT rowid, snippet(passage_index, 2, ?, ?, '…', ?) FROM passage_index "
        f"WHERE passage_index MATCH ? AND rowid IN ({marks})",
        (MARK_START, MARK_END, EXCERPT_TOKENS, expression, *ids)).fetchall()) if ids else {}
    details = {row[0]: row[1:] for row in conn.execute(
        f"SELECT p.id, d.key, d.kind, d.title, d.source, d.url, d.meta, p.section, p.anchor, p.page, "
        f"p.line_start, p.line_end FROM passages p JOIN documents d ON d.id = p.document_id "
        f"WHERE p.id IN ({marks})", ids)} if ids else {}

    hits = []
    for rank, (passage_id, document, score) in enumerate(chosen, start=1):
        key, kind, title, source, url, meta, section, anchor, page, line_start, line_end = details[passage_id]
        hits.append(Hit(
            rank=rank, score=-score, key=key, kind=kind, title=title, source=source,
            url=f"{url}#{anchor}" if url and anchor else url,
            source_url=source_url(json.loads(meta), line_start, line_end),
            section=section, page=page, line_start=line_start, line_end=line_end,
            excerpt=excerpts.get(passage_id, ""), more_in_document=per_doc[document] - 1,
        ))
    return Results(query, words, hits, len(matched), len(per_doc))
