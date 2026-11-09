"""Semantic search with LSA (latent semantic analysis), and its fusion with keyword search.

LSA represents each passage by TF-IDF weights over its words, then compresses those into
DIMENSIONS directions with a truncated SVD, so passages that use different words in similar
contexts can land near each other. It is a classical method, chosen because the build machine
can't download neural embedding models; it is called LSA here, not "embeddings".

The index is fitted on the library's own passages and kept in the library file as plain numpy
arrays (loaded with allow_pickle=False, so opening a library never runs code). It records a
fingerprint of the passages it was fitted on and is rebuilt when they change.

Hybrid search fuses the keyword and LSA document rankings with weighted reciprocal rank fusion:
a document's score is KEYWORD_WEIGHT / (FUSION_C + its keyword rank) + 1 / (FUSION_C + its LSA
rank). The settings were chosen on the tuning questions only (docs/evaluation.md).
"""

from __future__ import annotations

import io
import time

import numpy as np

from evfind.store import Library

# Chosen on the tuning questions with `evfind tune --method hybrid` (docs/evaluation.md):
# hybrid search scored MRR 0.788 at 100 dimensions, 0.787 at 400 and 500 (a tie in practice);
# ties go to fewer dimensions, which also makes the index a quarter of the size.
DIMENSIONS = 100
FUSION_C = 10
KEYWORD_WEIGHT = 2.0
SEMANTIC_ALONE_DIMENSIONS = 800  # LSA on its own did best here (0.680); used only to evaluate it fairly
SEED = 0                   # for the randomized SVD, so a rebuild gives the same index
FORMAT = 1                 # bump when the stored arrays change meaning


class SemanticUnavailable(Exception):
    """The library is too small, or too uniform, for an LSA index."""


def _vectorizer(vocabulary=None, min_df=1):
    from sklearn.feature_extraction.text import TfidfVectorizer

    return TfidfVectorizer(sublinear_tf=True, min_df=min_df, stop_words="english", dtype=np.float32,
                           vocabulary=vocabulary)


class SemanticIndex:
    def __init__(self, terms: np.ndarray, idf: np.ndarray, components: np.ndarray, passage_ids: np.ndarray,
                 document_ids: np.ndarray, vectors: np.ndarray, fingerprint: str):
        self.terms, self.idf, self.components = terms, idf, components
        self.passage_ids, self.document_ids = passage_ids, document_ids
        self.vectors, self.fingerprint = vectors, fingerprint
        self._vectorizer = _vectorizer({term: i for i, term in enumerate(terms.tolist())})
        self._vectorizer.idf_ = idf

    @classmethod
    def fit(cls, rows: list[tuple[int, int, str]], fingerprint: str, dimensions: int | None = None) -> SemanticIndex:
        """Fit on (passage id, document id, text) rows, with DIMENSIONS unless told otherwise."""
        from sklearn.decomposition import TruncatedSVD
        from sklearn.preprocessing import normalize

        if len(rows) < 3:
            raise SemanticUnavailable("semantic search needs a library of at least 3 passages")
        vectorizer = _vectorizer(min_df=2 if len(rows) >= 50 else 1)
        try:
            matrix = vectorizer.fit_transform([text for _, _, text in rows])
        except ValueError:  # every word is a stop word, or too rare
            raise SemanticUnavailable("the library's passages have too few distinct words for semantic search") from None
        k = min(dimensions or DIMENSIONS, matrix.shape[1] - 1, len(rows) - 1)
        if k < 2:
            raise SemanticUnavailable("the library's passages have too few distinct words for semantic search")
        svd = TruncatedSVD(n_components=k, random_state=SEED, n_iter=7)
        vectors = normalize(svd.fit_transform(matrix)).astype(np.float32)
        terms = np.array(sorted(vectorizer.vocabulary_, key=vectorizer.vocabulary_.get))
        return cls(terms, vectorizer.idf_.astype(np.float32), svd.components_.astype(np.float32),
                   np.array([pid for pid, _, _ in rows], dtype=np.int64),
                   np.array([doc for _, doc, _ in rows], dtype=np.int64), vectors, fingerprint)

    def similarities(self, query: str) -> np.ndarray:
        """The cosine similarity of the query to every passage, aligned with passage_ids (all 0 if
        no query word is in the index)."""
        projected = self._vectorizer.transform([query]) @ self.components.T
        norm = float(np.linalg.norm(projected))
        if norm == 0.0:
            return np.zeros(len(self.passage_ids), dtype=np.float32)
        return self.vectors @ (np.asarray(projected).ravel() / norm).astype(np.float32)

    def to_bytes(self) -> bytes:
        buffer = io.BytesIO()
        np.savez(buffer, format=np.array([FORMAT]), terms=self.terms, idf=self.idf, components=self.components,
                 passage_ids=self.passage_ids, document_ids=self.document_ids, vectors=self.vectors)
        return buffer.getvalue()

    @classmethod
    def from_bytes(cls, data: bytes, fingerprint: str) -> SemanticIndex:
        with np.load(io.BytesIO(data), allow_pickle=False) as arrays:
            if int(arrays["format"][0]) != FORMAT:
                raise ValueError("an index in an older format")
            return cls(arrays["terms"], arrays["idf"], arrays["components"], arrays["passage_ids"],
                       arrays["document_ids"], arrays["vectors"], fingerprint)


def fingerprint(library: Library) -> str:
    """What the index depends on: the passages (re-importing gives them new ids) and the settings."""
    count, top, total, length = library.conn.execute(
        "SELECT count(*), coalesce(max(id), 0), coalesce(sum(id), 0), coalesce(sum(length(text)), 0) "
        "FROM passages").fetchone()
    return f"{FORMAT}:{DIMENSIONS}:{SEED}:{count}:{top}:{total}:{length}"


def _rows(library: Library) -> list[tuple[int, str]]:
    return library.conn.execute(
        "SELECT p.id, p.document_id, d.title || char(10) || p.section || char(10) || p.text FROM passages p "
        "JOIN documents d ON d.id = p.document_id ORDER BY p.id").fetchall()


def load_index(library: Library, *, progress=None) -> SemanticIndex:
    """The library's LSA index: the stored one if it is current, otherwise fitted now and stored."""
    current = fingerprint(library)
    cached = getattr(library, "_semantic", None)
    if cached is not None and cached.fingerprint == current:
        return cached
    row = library.conn.execute("SELECT fingerprint, data FROM semantic_index WHERE id = 1").fetchone()
    index = None
    if row is not None and row[0] == current:
        try:
            index = SemanticIndex.from_bytes(row[1], current)
        except (ValueError, KeyError, OSError):
            index = None  # unreadable or an old format: build it again
    if index is None:
        started = time.perf_counter()
        if progress:
            progress("building the semantic index (once per change to the library)…")
        index = SemanticIndex.fit(_rows(library), current)
        with library.conn:
            library.conn.execute("INSERT OR REPLACE INTO semantic_index (id, fingerprint, data) VALUES (1, ?, ?)",
                                 (current, index.to_bytes()))
        if progress:
            progress(f"built in {time.perf_counter() - started:.1f} s")
    library._semantic = index
    return index


def rank_passages(index: SemanticIndex, query: str) -> list[tuple[int, int, float]]:
    """(passage id, document id, similarity) for every passage with a positive similarity, best
    first; ties by passage id."""
    sims = index.similarities(query)
    order = np.lexsort((index.passage_ids, -sims))
    return [(int(index.passage_ids[i]), int(index.document_ids[i]), float(sims[i])) for i in order if sims[i] > 0]


def fuse(keyword: list, semantic: list, *, c: float = FUSION_C,
         keyword_weight: float = KEYWORD_WEIGHT) -> list[tuple[object, float]]:
    """Weighted reciprocal rank fusion of two rankings of the same kind of item: (item, score),
    best first. Ties keep the order of first appearance, keyword ranking first."""
    scores: dict = {}
    for weight, ranking in ((keyword_weight, keyword), (1.0, semantic)):
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + weight / (c + rank)
    order = {item: i for i, item in enumerate(dict.fromkeys([*keyword, *semantic]))}
    return [(item, scores[item]) for item in sorted(scores, key=lambda item: (-scores[item], order[item]))]
