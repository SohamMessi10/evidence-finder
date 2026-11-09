import sqlite3

import pytest

from evfind.extract import Extracted, Passage
from evfind.store import SCHEMA_VERSION, Library, StoreError


@pytest.fixture
def library(tmp_path):
    with Library.open(tmp_path / "lib.db", create=True) as lib:
        yield lib


def two_passages():
    return Extracted(passages=[
        Passage(0, "First passage.", "Intro", "intro", None, 3, 5),
        Passage(1, "Second passage.", "Details", "details", None, 7, 9),
    ])


def test_a_new_library_has_the_schema(tmp_path):
    with Library.open(tmp_path / "lib.db", create=True) as lib:
        assert lib.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert lib.stats() == {"documents": 0, "indexed": 0, "unusable": 0, "passages": 0, "words": 0,
                               "with_problems": 0}


def test_opening_a_missing_library_says_how_to_make_one(tmp_path):
    with pytest.raises(StoreError, match="evfind ingest --peps"):
        Library.open(tmp_path / "missing.db")
    assert not (tmp_path / "missing.db").exists()


def test_a_sqlite_file_from_something_else_is_refused(tmp_path):
    path = tmp_path / "other.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE notes (body TEXT)")
    conn.close()
    with pytest.raises(StoreError, match="not an Evidence Finder library"):
        Library.open(path)


def test_a_library_from_another_version_is_refused(tmp_path):
    Library.open(tmp_path / "lib.db", create=True).close()
    conn = sqlite3.connect(tmp_path / "lib.db")
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(StoreError, match=f"schema 99, expected {SCHEMA_VERSION}"):
        Library.open(tmp_path / "lib.db")


def test_a_file_that_isnt_sqlite_is_refused(tmp_path):
    (tmp_path / "junk.db").write_bytes(b"this is not a database, just some bytes" * 50)
    with pytest.raises(StoreError, match="isn't a usable library"):
        Library.open(tmp_path / "junk.db")


def test_put_stores_a_document_and_its_passages(library):
    library.put("pep-0001", "pep", "PEP 1: Purpose", "peps/pep-0001.rst", two_passages(),
                url="https://peps.python.org/pep-0001/", meta={"number": 1})
    assert library.passages("pep-0001") == two_passages().passages
    doc = library.document("pep-0001")
    assert doc == {"key": "pep-0001", "kind": "pep", "title": "PEP 1: Purpose", "url": "https://peps.python.org/pep-0001/",
                   "source": "peps/pep-0001.rst", "meta": {"number": 1}, "status": "indexed", "problems": []}
    assert library.stats()["words"] == 4


def test_a_missing_document_is_none(library):
    assert library.document("nope") is None
    assert library.passages("nope") == []


def test_putting_a_document_again_replaces_it(library):
    library.put("file:a", "text", "a", "a.txt", two_passages())
    library.put("file:a", "text", "a", "a.txt",
                Extracted(passages=[Passage(0, "Only one now.")], problems=["a warning"]))
    assert [p.text for p in library.passages("file:a")] == ["Only one now."]
    assert library.document("file:a")["problems"] == ["a warning"]
    assert library.stats()["documents"] == 1 and library.stats()["passages"] == 1


def test_an_unusable_document_is_kept_with_its_reason(library):
    library.put("file:scan", "pdf", "scan", "scan.pdf", Extracted(unusable="no page has extractable text"))
    doc = library.document("file:scan")
    assert (doc["status"], doc["problems"]) == ("unusable", ["no page has extractable text"])
    assert library.stats() | {} == {"documents": 1, "indexed": 0, "unusable": 1, "passages": 0, "words": 0,
                                    "with_problems": 1}


def test_a_failed_put_leaves_the_earlier_copy_alone(library):
    library.put("file:a", "text", "a", "a.txt", two_passages())
    broken = Extracted(passages=[Passage(0, "fine"), Passage(1, "   ")])  # blank text breaks a CHECK
    with pytest.raises(sqlite3.IntegrityError):
        library.put("file:a", "text", "a", "a.txt", broken)
    assert library.passages("file:a") == two_passages().passages


def test_deleting_a_document_deletes_its_passages_and_problems(library):
    library.put("file:a", "text", "a", "a.txt", Extracted(passages=[Passage(0, "x")], problems=["p"]))
    with library.conn:
        library.conn.execute("DELETE FROM documents")
    assert library.conn.execute("SELECT count(*) FROM passages").fetchone()[0] == 0
    assert library.conn.execute("SELECT count(*) FROM problems").fetchone()[0] == 0


@pytest.mark.parametrize(("column", "value"), [
    ("page", 0), ("line_start", 0), ("line_end", 1), ("ordinal", -1), ("text", ""), ("line_start", "three"),
])
def test_impossible_passages_are_rejected(library, column, value):
    doc = library.put("file:a", "text", "a", "a.txt", Extracted())
    row = {"document_id": doc, "ordinal": 0, "text": "x", "page": None, "line_start": 5, "line_end": 6}
    row[column] = value
    with pytest.raises(sqlite3.IntegrityError), library.conn:
        library.conn.execute(f"INSERT INTO passages ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                             tuple(row.values()))


@pytest.mark.parametrize(("column", "value"), [("kind", "docx"), ("status", "maybe"), ("meta", "{not json")])
def test_impossible_documents_are_rejected(library, column, value):
    row = {"key": "k", "kind": "text", "title": "t", "source": "s", "meta": "{}", "status": "indexed"}
    row[column] = value
    with pytest.raises(sqlite3.IntegrityError), library.conn:
        library.conn.execute(f"INSERT INTO documents ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                             tuple(row.values()))


def test_a_passage_needs_an_existing_document(library):
    with pytest.raises(sqlite3.IntegrityError), library.conn:
        library.conn.execute("INSERT INTO passages (document_id, ordinal, text) VALUES (12345, 0, 'x')")


def indexed(library):
    return library.conn.execute("SELECT rowid, title, section, text FROM passage_index ORDER BY rowid").fetchall()


def test_the_search_index_follows_every_change(library):
    library.put("file:a", "text", "Old title", "a.txt", two_passages())
    first, second = [row[0] for row in library.conn.execute("SELECT id FROM passages ORDER BY id")]
    assert indexed(library) == [(first, "Old title", "Intro", "First passage."),
                                (second, "Old title", "Details", "Second passage.")]
    with library.conn:
        library.conn.execute("UPDATE passages SET text = 'Edited.' WHERE id = ?", (first,))
        library.conn.execute("UPDATE documents SET title = 'New title'")
    assert indexed(library) == [(first, "New title", "Intro", "Edited."),
                                (second, "New title", "Details", "Second passage.")]
    with library.conn:
        library.conn.execute("DELETE FROM documents")  # the passages go by ON DELETE CASCADE
    assert indexed(library) == []


def test_a_milestone_1_library_is_upgraded_in_place(tmp_path):
    path = tmp_path / "lib.db"
    with Library.open(path, create=True) as lib:
        lib.put("file:a", "text", "A", "a.txt", two_passages())
        lib.conn.executescript("DROP TRIGGER passage_added; DROP TRIGGER passage_removed; "
                               "DROP TRIGGER passage_changed; DROP TRIGGER document_retitled; "
                               "DROP TABLE passage_index; PRAGMA user_version = 1;")
    with Library.open(path) as lib:
        assert lib.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert [row[3] for row in indexed(lib)] == ["First passage.", "Second passage."]
        lib.put("file:b", "text", "B", "b.txt", Extracted(passages=[Passage(0, "Third.")]))
        assert len(indexed(lib)) == 3  # and the triggers are there for what comes next


def test_a_failed_upgrade_changes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "lib.db"
    with Library.open(path, create=True) as lib:
        lib.put("file:a", "text", "A", "a.txt", two_passages())
        lib.conn.executescript("DROP TRIGGER passage_added; DROP TRIGGER passage_removed; "
                               "DROP TRIGGER passage_changed; DROP TRIGGER document_retitled; "
                               "DROP TABLE passage_index; PRAGMA user_version = 1;")
    monkeypatch.setattr("evfind.store.FILL_SEARCH_INDEX", "INSERT INTO no_such_table VALUES (1);")
    with pytest.raises(StoreError, match="isn't a usable library"):
        Library.open(path)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name = 'passage_index'").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM passages").fetchone()[0] == 2
    finally:
        conn.close()


def test_without_fts5_the_error_says_why(tmp_path, monkeypatch):
    from evfind import store

    monkeypatch.setattr(store, "SEARCH_INDEX", store.SEARCH_INDEX.replace("USING fts5(", "USING fts5_missing("))
    with pytest.raises(StoreError, match="built without FTS5"):
        Library.open(tmp_path / "lib.db", create=True)


def test_two_passages_cant_share_a_position(library):
    doc = library.put("file:a", "text", "a", "a.txt", Extracted())
    with pytest.raises(sqlite3.IntegrityError), library.conn:
        library.conn.executemany("INSERT INTO passages (document_id, ordinal, text) VALUES (?, 0, 'x')", [(doc,), (doc,)])
