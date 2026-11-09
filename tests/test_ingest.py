import pytest

from evfind.extract import segment_rst
from evfind.ingest import DEFAULT_SNAPSHOT, ingest_files, ingest_peps
from evfind.peps import parse_header, read_snapshot
from evfind.store import Library


@pytest.fixture(scope="module")
def pep_library(tmp_path_factory):
    path = tmp_path_factory.mktemp("lib") / "peps.db"
    with Library.open(path, create=True) as lib:
        report, header = ingest_peps(lib)
    return path, report, header


def test_the_snapshot_ships_inside_the_package():
    assert DEFAULT_SNAPSHOT.is_file()
    assert DEFAULT_SNAPSHOT.parent.name == "data" and DEFAULT_SNAPSHOT.parent.parent.name == "evfind"


def test_ingest_peps_imports_every_pep_with_all_its_passages(pep_library):
    path, report, header = pep_library
    _, peps = read_snapshot(DEFAULT_SNAPSHOT)
    expected = sum(len(segment_rst(p.text, parse_header(p.text)[1])) for p in peps)
    assert (len(report.indexed), report.passages, report.unusable) == (712, expected, [])
    with Library.open(path) as lib:
        stats = lib.stats()
    assert (stats["documents"], stats["indexed"], stats["passages"], stats["with_problems"]) == (712, 712, expected, 0)
    assert header["commit"].startswith("d94bba4")


def test_a_pep_keeps_its_reference_and_metadata(pep_library):
    with Library.open(pep_library[0]) as lib:
        doc = lib.document("pep-0572")
        passages = lib.passages("pep-0572")
    assert doc["title"] == "PEP 572: Assignment Expressions"
    assert doc["url"] == "https://peps.python.org/pep-0572/"
    assert doc["meta"]["number"] == 572 and doc["meta"]["snapshot_commit"].startswith("d94bba4")
    assert doc["meta"]["path_in_repo"] == "peps/pep-0572.rst"
    abstract = next(p for p in passages if p.section == "Abstract")
    assert abstract.anchor == "abstract"
    assert "assign to variables within an expression" in " ".join(abstract.text.split())


def small_snapshot(path, count=25):
    """The first `count` bundled PEPs as a snapshot of their own."""
    import gzip
    import json
    from dataclasses import asdict

    header, peps = read_snapshot(DEFAULT_SNAPSHOT)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(header) + "\n")
        for pep in peps[:count]:
            handle.write(json.dumps(asdict(pep)) + "\n")
    return path


def test_importing_the_peps_twice_doesnt_duplicate_them(tmp_path):
    snapshot = small_snapshot(tmp_path / "small.jsonl.gz")
    with Library.open(tmp_path / "twice.db", create=True) as lib:
        once, _ = ingest_peps(lib, snapshot)
        first = lib.stats()
        ingest_peps(lib, snapshot)
        assert lib.stats() == first and first["documents"] == 25 and first["passages"] == once.passages
        assert lib.conn.execute("SELECT count(*) FROM passage_index").fetchone()[0] == once.passages


def test_ingest_files_reports_what_it_could_and_couldnt_use(tmp_path):
    (tmp_path / "notes.md").write_text("# Notes\n\nUseful text.\n", encoding="utf-8")
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")
    (tmp_path / "report.docx").write_bytes(b"PK\x03\x04")
    paths = [tmp_path / "notes.md", tmp_path / "empty.txt", tmp_path / "report.docx"]
    with Library.open(tmp_path / "lib.db", create=True) as lib:
        report = ingest_files(lib, paths)
        assert report.indexed == [str(tmp_path / "notes.md")]
        assert [source for source, _ in report.unusable] == [str(tmp_path / "empty.txt"), str(tmp_path / "report.docx")]
        assert report.passages == 1
        doc = lib.document(f"file:{(tmp_path / 'notes.md').resolve()}")
        assert (doc["kind"], doc["title"], doc["status"]) == ("markdown", "notes", "indexed")
        unusable = lib.document(f"file:{(tmp_path / 'report.docx').resolve()}")
        assert unusable["status"] == "unusable" and ".docx isn't supported" in unusable["problems"][0]


def test_the_same_file_by_two_paths_is_one_document(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with Library.open(tmp_path / "lib.db", create=True) as lib:
        ingest_files(lib, [tmp_path / "a.txt"])
        from pathlib import Path
        ingest_files(lib, [Path("a.txt")])
        assert lib.stats()["documents"] == 1
