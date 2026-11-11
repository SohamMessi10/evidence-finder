import sqlite3

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from evfind.extract import Extracted, Passage
from evfind.search import (
    MARK_END,
    MARK_START,
    MAX_QUERY_WORDS,
    SearchError,
    match_expression,
    render_excerpt,
    search,
    source_url,
)
from evfind.store import Library


@pytest.fixture
def library():
    with Library.open(":memory:", create=True) as lib:
        yield lib


def add(library, key, title, *texts, section="", anchor="", url="", meta=None, page=None):
    passages = [Passage(i, text, section, anchor, page, 10 * i + 1, 10 * i + 5) for i, text in enumerate(texts)]
    library.put(key, "text", title, f"{key}.txt", Extracted(passages=passages), url=url, meta=meta)


# --- turning a query into an FTS5 expression ------------------------------------------------


def test_each_distinct_word_is_quoted_and_joined_with_or():
    assert match_expression("Walrus, walrus OPERATOR?") == ('"walrus" OR "operator"', ("walrus", "operator"))


def test_underscores_and_punctuation_separate_words():
    assert match_expression("__future__ import; PEP-8 := x")[1] == ("future", "import", "pep", "8", "x")


def test_a_long_query_keeps_its_first_words():
    words = match_expression(" ".join(f"w{i}" for i in range(100)))[1]
    assert words == tuple(f"w{i}" for i in range(MAX_QUERY_WORDS))


@pytest.mark.parametrize("query", ["", "   ", "?!", ":= -> ** //", "___"])
def test_a_query_without_words_is_refused(query):
    with pytest.raises(SearchError, match="nothing to search for"):
        match_expression(query)


SYNTAX = st.sampled_from(['"', "'", "*", "^", ":", "(", ")", "-", "+", "AND", "OR", "NOT", "NEAR", "NEAR(", "{", "}"])


@settings(max_examples=300, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(st.one_of(st.text(max_size=12), SYNTAX), max_size=12).map(" ".join))
def test_nothing_typed_can_break_the_query(text):
    """Whatever is typed, the expression is valid FTS5: a search either runs or says there's
    nothing to search for. (Raw text fails: "what is := ?" gives "no such column: is".)"""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(title, section, text, "
                     "tokenize='porter unicode61 remove_diacritics 2')")
        conn.execute("INSERT INTO t VALUES ('a title', 'a section', 'some text with words')")
        try:
            expression, _ = match_expression(text)
        except SearchError:
            return
        conn.execute("SELECT count(*) FROM t WHERE t MATCH ?", (expression,)).fetchone()
    finally:
        conn.close()


# --- finding and ranking --------------------------------------------------------------------


def test_only_matching_passages_are_found_best_first(library):
    add(library, "a", "Doc A", "The walrus operator assigns a value inside an expression.")
    add(library, "b", "Doc B", "Operator overloading lets classes define what + means.")
    add(library, "c", "Doc C", "Nothing relevant is said here.")
    results = search(library, "walrus operator")
    assert [h.key for h in results.hits] == ["a", "b"]
    assert (results.passages_matched, results.documents_matched) == (2, 2)
    assert results.hits[0].score > results.hits[1].score > 0
    assert [h.rank for h in results.hits] == [1, 2]


def test_stemming_case_and_diacritics_are_ignored(library):
    add(library, "a", "Doc A", "Assignment EXPRESSIONS were added.")
    add(library, "b", "Doc B", "Ordering a café au lait.")
    add(library, "c", "Doc C", "Tiếng Việt")  # two accents on one letter: needs remove_diacritics 2
    assert [h.key for h in search(library, "expression").hits] == ["a"]
    assert [h.key for h in search(library, "cafe").hits] == ["b"]
    assert [h.key for h in search(library, "tieng viet").hits] == ["c"]


def test_titles_and_sections_are_searched_too(library):
    add(library, "a", "Pattern Matching", "Nothing in the text.")
    add(library, "b", "Something else", "Nor here.", section="Walrus patterns")
    assert [h.key for h in search(library, "matching").hits] == ["a"]
    assert [h.key for h in search(library, "walrus").hits] == ["b"]


def test_by_default_each_document_gives_its_best_passage(library):
    add(library, "a", "Doc A", "walrus walrus walrus", "walrus here", "a walrus once", "no match")
    add(library, "b", "Doc B", "one walrus")
    grouped = search(library, "walrus")
    assert [(h.key, h.more_in_document) for h in grouped.hits] == [("a", 2), ("b", 0)]
    assert grouped.hits[0].excerpt.count(MARK_START) == 3        # the best passage of the three
    assert len(search(library, "walrus", per_document=None).hits) == 4
    assert [h.key for h in search(library, "walrus", per_document=2).hits] == ["a", "a", "b"]
    assert len(search(library, "walrus", limit=1).hits) == 1


def test_equal_scores_keep_a_fixed_order(library):
    for key in "cab":
        add(library, key, "Same", "identical walrus text")
    assert [h.key for h in search(library, "walrus").hits] == ["c", "a", "b"]  # the order they were added


def test_the_excerpt_marks_the_matched_words(library):
    add(library, "a", "Doc A", "First the Walrus operator, then nothing.")
    excerpt = search(library, "walrus").hits[0].excerpt
    assert f"{MARK_START}Walrus{MARK_END}" in excerpt
    assert render_excerpt(excerpt, "<b>", "</b>") == "First the <b>Walrus</b> operator, then nothing."


def test_the_excerpt_is_always_from_the_passage_text(library):
    """The title and section are shown on their own, so a title-only match still excerpts the text."""
    add(library, "a", "The Walrus Guide", "Nothing about it in the text.")
    assert search(library, "walrus").hits[0].excerpt == "Nothing about it in the text."


def test_common_words_are_matched_but_not_marked(library):
    add(library, "a", "Doc A", "When a walrus does arrive, the keeper is ready.")
    hit = search(library, "when does a walrus arrive").hits[0]
    # "when" and "a" are in scikit-learn's English stop words; "does" isn't, so it stays marked
    assert hit.excerpt == (f"When a {MARK_START}walrus{MARK_END} {MARK_START}does{MARK_END} "
                           f"{MARK_START}arrive{MARK_END}, the keeper is ready.")


def test_render_excerpt_puts_it_on_one_line():
    assert render_excerpt(f"one\n\n  two {MARK_START}three{MARK_END}\tfour", "[", "]") == "one two [three] four"


def test_a_replaced_document_leaves_nothing_stale(library):
    add(library, "a", "Doc A", "zebra")
    add(library, "a", "Doc A", "giraffe")
    assert search(library, "zebra").hits == []
    assert [h.key for h in search(library, "giraffe").hits] == ["a"]


# --- references -------------------------------------------------------------------------------


def test_the_link_goes_to_the_section_when_its_anchor_is_known(library):
    add(library, "a", "Doc A", "walrus", section="Intro", anchor="intro", url="https://example.org/a/")
    add(library, "b", "Doc B", "walrus", section="Repeated", anchor="", url="https://example.org/b/")
    add(library, "c", "Doc C", "walrus")
    links = {h.key: h.url for h in search(library, "walrus").hits}
    assert links == {"a": "https://example.org/a/#intro", "b": "https://example.org/b/", "c": ""}


def test_a_pep_passage_links_to_its_exact_lines_at_the_snapshot_commit():
    meta = {"snapshot_commit": "d94bba4a5dff241fd418f30223d3bf05de27a771", "path_in_repo": "peps/pep-0572.rst"}
    assert source_url(meta, 17, 28) == ("https://github.com/python/peps/blob/d94bba4a5dff241fd418f30223d3bf05de27a771/"
                                        "peps/pep-0572.rst?plain=1#L17-L28")
    assert source_url(meta, 5, 5).endswith("?plain=1#L5")
    assert source_url({}, 5, 9) == "" and source_url(meta, None, None) == ""


def test_place_describes_where_the_passage_is(library):
    add(library, "a", "Doc A", "walrus", section="Intro")
    add(library, "b", "Doc B", "walrus", page=3)
    places = {h.key: h.place for h in search(library, "walrus").hits}
    assert places == {"a": "Intro · lines 1–5", "b": "page 3 · lines 1–5"}
    library.put("c", "pdf", "Doc C", "c.pdf", Extracted(passages=[Passage(0, "walrus", page=2)]))
    library.put("d", "text", "Doc D", "d.txt", Extracted(passages=[Passage(0, "walrus", line_start=7, line_end=7)]))
    places = {h.key: h.place for h in search(library, "walrus").hits}
    assert (places["c"], places["d"]) == ("page 2", "line 7")


@pytest.mark.parametrize(("arguments", "message"), [
    ({"limit": 0}, "between 1 and 100"), ({"limit": 101}, "between 1 and 100"),
    ({"per_document": 0}, "at least 1"),
    ({"weights": (1.0, 0.0, 1.0)}, "positive"), ({"weights": (1.0, 1.0)}, "positive"),
])
def test_bad_settings_are_refused(library, arguments, message):
    with pytest.raises(SearchError, match=message):
        search(library, "walrus", **arguments)


# --- the index stays in step with the passages ------------------------------------------------

WORDS = st.lists(st.sampled_from(["walrus", "operator", "pattern", "match", "lambda", "yield"]), min_size=1, max_size=4)
OPERATIONS = st.lists(st.tuples(st.sampled_from(["a", "b", "c"]), st.lists(WORDS.map(" ".join), max_size=3)),
                      min_size=1, max_size=12)


@settings(max_examples=60, deadline=None)
@given(OPERATIONS)
def test_the_index_always_matches_the_passages(operations):
    """Any sequence of imports and re-imports (an empty one leaves a document with no passages):
    the index holds exactly the passages, with their current title, section and text."""
    with Library.open(":memory:", create=True) as lib:
        for key, texts in operations:
            add(lib, key, f"Title of {key} {len(texts)}", *texts, section=f"section {key}")
            expected = lib.conn.execute(
                "SELECT p.id, d.title, p.section, p.text FROM passages p JOIN documents d ON d.id = p.document_id "
                "ORDER BY p.id").fetchall()
            indexed = lib.conn.execute("SELECT rowid, title, section, text FROM passage_index ORDER BY rowid").fetchall()
            assert indexed == expected


# --- the PEPs ---------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def peps(pep_library):
    with Library.open(pep_library) as lib:
        yield lib


def test_searching_the_peps_by_title_words_finds_the_pep(peps):
    """A functional check, not a measure of quality: milestone 3 measures that on questions
    written for it. (PEP 379, the withdrawn first proposal, ranks first here; that's fair.)"""
    hits = search(peps, "assignment expressions", limit=3).hits
    pep = next(h for h in hits if h.key == "pep-0572")
    assert pep.title == "PEP 572: Assignment Expressions"
    assert pep.url.startswith("https://peps.python.org/pep-0572/")


def test_pep_hits_are_ranked_grouped_and_referenced(peps):
    results = search(peps, "structural pattern matching tutorial", limit=20)
    hits = results.hits
    assert len(hits) == 20 and len({h.key for h in hits}) == 20
    assert [h.rank for h in hits] == list(range(1, 21))
    assert all(a.score >= b.score for a, b in zip(hits, hits[1:]))
    for hit in hits:
        lines = f"L{hit.line_start}" if hit.line_start == hit.line_end else f"L{hit.line_start}-L{hit.line_end}"
        assert hit.source_url == ("https://github.com/python/peps/blob/d94bba4a5dff241fd418f30223d3bf05de27a771/"
                                  f"peps/{hit.key}.rst?plain=1#{lines}")
        assert hit.url.startswith(f"https://peps.python.org/{hit.key}/")
    assert results.documents_matched > 20 and results.passages_matched >= results.documents_matched
