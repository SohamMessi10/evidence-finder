import io
import itertools

import numpy as np
import pytest

from evfind import semantic
from evfind.extract import Extracted, Passage
from evfind.search import MARK_START, SearchError, search
from evfind.semantic import (
    SemanticIndex,
    SemanticUnavailable,
    fingerprint,
    fuse,
    load_index,
    rank_passages,
)
from evfind.store import Library

VEHICLES = ["car engine wheels road", "automobile engine fuel road", "car automobile driver road",
            "truck engine wheels fuel", "car driver wheels", "automobile wheels driver fuel"]
FRUIT = ["apple banana orchard fruit", "banana fruit market sweet", "apple orchard harvest fruit",
         "cherry apple sweet fruit", "banana cherry market harvest", "orchard fruit sweet harvest"]


def library_of(texts):
    lib = Library.open(":memory:", create=True)
    for i, text in enumerate(texts):
        lib.put(f"d{i}", "text", f"Doc {i}", f"d{i}.txt", Extracted(passages=[Passage(0, text)]))
    return lib


def rows(texts):
    return [(i + 1, i + 1, text) for i, text in enumerate(texts)]


# --- the LSA index ---------------------------------------------------------------------------


def test_lsa_finds_passages_by_related_words():
    """'automobile' never appears with 'truck', but both appear with engine, wheels and fuel."""
    index = SemanticIndex.fit(rows(VEHICLES + FRUIT), "f", dimensions=2)
    ranked = [pid for pid, _, _ in rank_passages(index, "automobile")]
    assert set(ranked[:6]) == {1, 2, 3, 4, 5, 6}           # every vehicle passage before any fruit one
    truck = VEHICLES.index("truck engine wheels fuel") + 1
    assert truck in ranked[:6]                              # including the one without the word


def test_a_query_with_no_known_word_finds_nothing():
    index = SemanticIndex.fit(rows(VEHICLES + FRUIT), "f", dimensions=2)
    assert not index.similarities("zzz qqq").any()
    assert rank_passages(index, "zzz qqq") == []


def test_equal_similarities_are_ordered_by_passage_id():
    texts = ["walrus operator"] * 40 + FRUIT
    ranked = rank_passages(SemanticIndex.fit(rows(texts), "f", dimensions=2), "walrus")
    tied = [pid for pid, _, sim in ranked if sim == ranked[0][2]]
    assert len(tied) == 40 and tied == sorted(tied)


def test_fitting_is_deterministic():
    a = SemanticIndex.fit(rows(VEHICLES + FRUIT), "f", dimensions=3)
    b = SemanticIndex.fit(rows(VEHICLES + FRUIT), "f", dimensions=3)
    assert np.array_equal(a.vectors, b.vectors) and np.array_equal(a.components, b.components)


def test_the_index_survives_a_round_trip_through_bytes():
    index = SemanticIndex.fit(rows(VEHICLES + FRUIT), "f", dimensions=3)
    again = SemanticIndex.from_bytes(index.to_bytes(), "f")
    for name in ("terms", "idf", "components", "passage_ids", "document_ids", "vectors"):
        assert np.array_equal(getattr(index, name), getattr(again, name)), name
    assert np.array_equal(index.similarities("car fuel"), again.similarities("car fuel"))


def test_stored_arrays_are_never_unpickled():
    buffer = io.BytesIO()
    np.savez(buffer, format=np.array([semantic.FORMAT]), terms=np.array([{"evil": 1}], dtype=object))
    with pytest.raises(ValueError, match="pickle"):
        SemanticIndex.from_bytes(buffer.getvalue(), "f")


def test_an_index_in_another_format_is_refused():
    index = SemanticIndex.fit(rows(VEHICLES + FRUIT), "f", dimensions=2)
    data = io.BytesIO()
    np.savez(data, format=np.array([semantic.FORMAT + 1]), terms=index.terms, idf=index.idf,
             components=index.components, passage_ids=index.passage_ids, document_ids=index.document_ids,
             vectors=index.vectors)
    with pytest.raises(ValueError, match="older format"):
        SemanticIndex.from_bytes(data.getvalue(), "f")


@pytest.mark.parametrize("texts", [["one passage", "two passages"], ["the and of", "a an the", "is it to"],
                                   ["walrus", "walrus", "walrus"]])  # one word: no room for 2 dimensions
def test_too_small_or_empty_libraries_are_refused(texts):
    with pytest.raises(SemanticUnavailable):
        SemanticIndex.fit(rows(texts), "f")


# --- storing, reusing and rebuilding ---------------------------------------------------------


def test_the_index_is_built_once_stored_and_reused(monkeypatch):
    with library_of(VEHICLES + FRUIT) as lib:
        messages = []
        first = load_index(lib, progress=messages.append)
        assert messages[0].startswith("building the semantic index") and messages[1].startswith("built in")
        assert lib.conn.execute("SELECT fingerprint FROM semantic_index").fetchone()[0] == fingerprint(lib)
        lib._semantic = None  # forget the in-memory copy: the stored one must be used, not refitted
        monkeypatch.setattr(SemanticIndex, "fit", classmethod(lambda *a, **k: pytest.fail("refitted")))
        again = load_index(lib)
        assert np.array_equal(first.vectors, again.vectors)
        assert load_index(lib) is again  # and then kept in memory


def test_changing_the_library_rebuilds_the_index():
    with library_of(VEHICLES + FRUIT) as lib:
        before = load_index(lib)
        lib.put("new", "text", "New", "new.txt", Extracted(passages=[Passage(0, "car fuel road")]))
        after = load_index(lib)
        assert after.fingerprint != before.fingerprint and len(after.passage_ids) == len(before.passage_ids) + 1


def test_an_unreadable_stored_index_is_rebuilt():
    with library_of(VEHICLES + FRUIT) as lib:
        load_index(lib)
        with lib.conn:
            lib.conn.execute("UPDATE semantic_index SET data = x'00010203'")
        lib._semantic = None
        assert len(load_index(lib).passage_ids) == 12


def test_the_fingerprint_follows_the_passages():
    with library_of(VEHICLES) as lib:
        a = fingerprint(lib)
        lib.put("d0", "text", "Doc 0", "d0.txt", Extracted(passages=[Passage(0, "car engine wheels road")]))
        assert fingerprint(lib) != a  # same text, but re-imported: new passage ids


# --- fusion -----------------------------------------------------------------------------------


def test_fusion_adds_weighted_reciprocal_ranks():
    fused = dict(fuse(["a", "b", "c"], ["c", "a"], c=10, keyword_weight=2))
    assert fused == pytest.approx({"a": 2 / 11 + 1 / 12, "b": 2 / 12, "c": 2 / 13 + 1 / 11})
    assert [item for item, _ in fuse(["a", "b", "c"], ["c", "a"], c=10, keyword_weight=2)] == ["a", "c", "b"]


def test_fusion_ties_keep_the_keyword_order_first():
    assert [item for item, _ in fuse(["a", "b"], ["b", "a"], c=10, keyword_weight=1)] == ["a", "b"]


# --- searching by meaning ---------------------------------------------------------------------


@pytest.fixture
def mixed(monkeypatch):
    """Two topics; 2 LSA dimensions, since 100 can't generalise over 12 passages."""
    monkeypatch.setattr(semantic, "DIMENSIONS", 2)
    with library_of(VEHICLES + FRUIT) as lib:
        yield lib
        lib.close()


def test_semantic_search_ranks_by_meaning_and_shows_the_opening_without_a_match(mixed):
    results = search(mixed, "automobile", method="semantic", limit=6)
    assert results.method == "semantic"
    assert {h.key for h in results.hits} == {f"d{i}" for i in range(6)}
    truck = next(h for h in results.hits if h.key == "d3")
    assert truck.excerpt == "truck engine wheels fuel"           # no query word to mark: its opening
    assert all(h.more_in_document == 0 for h in results.hits)
    assert [h.score for h in results.hits] == sorted((h.score for h in results.hits), reverse=True)


def test_hybrid_search_puts_keyword_matches_first_and_adds_related_ones(mixed):
    hits = search(mixed, "automobile", method="hybrid", limit=12).hits
    keys = [h.key for h in hits]
    assert set(keys[:3]) == {"d1", "d2", "d5"}                    # the passages with the word
    assert "d3" in keys[:6]                                       # then related ones, like the truck
    assert all(a.score >= b.score for a, b in itertools.pairwise(hits))


def test_hybrid_shows_the_keyword_passage_of_a_document_that_matched_words(monkeypatch):
    """Doc "x": a pure vehicle passage, nearest in meaning, and one with the word itself."""
    monkeypatch.setattr(semantic, "DIMENSIONS", 2)
    with library_of(VEHICLES + FRUIT) as lib:
        lib.put("x", "text", "X", "x.txt", Extracted(passages=[
            Passage(0, "car driver wheels road fuel engine"), Passage(1, "automobile apple banana cherry")]))
        by_meaning = next(h for h in search(lib, "automobile", method="semantic", limit=20).hits if h.key == "x")
        hybrid = next(h for h in search(lib, "automobile", method="hybrid", limit=20).hits if h.key == "x")
    assert by_meaning.excerpt.startswith("car driver")
    assert f"{MARK_START}automobile" in hybrid.excerpt


@pytest.mark.parametrize("method", ["semantic", "hybrid"])
def test_all_passages_mode_works_for_every_method(mixed, method):
    hits = search(mixed, "fruit orchard", method=method, per_document=None, limit=4).hits
    assert len(hits) == 4 and hits[0].key in {"d6", "d8", "d11"}


def test_an_unknown_method_is_refused(mixed):
    with pytest.raises(SearchError, match="method must be one of"):
        search(mixed, "car", method="neural")


def test_semantic_search_on_a_tiny_library_says_why_not():
    with library_of(["only one passage", "and another"]) as lib:
        with pytest.raises(SearchError, match="at least 3 passages"):
            search(lib, "passage", method="semantic")
        assert search(lib, "passage").hits  # keyword search still works


def test_semantic_search_with_unknown_words_finds_nothing(mixed):
    assert search(mixed, "zzzq", method="semantic").hits == []
