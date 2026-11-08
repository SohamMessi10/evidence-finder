import gzip
import json
import subprocess

import pytest

from evfind.ingest import DEFAULT_SNAPSHOT
from evfind.peps import (
    BARE_ADDRESS,
    BRACKETED_ADDRESS,
    CorpusError,
    build_snapshot,
    copyright_section,
    is_permitted,
    parse_header,
    read_pep,
    read_snapshot,
    redact_header,
    without_emails,
)

PUBLIC_DOMAIN = "This document has been placed in the public domain."
CC0 = "This document is placed in the public domain or under the CC0-1.0-Universal license, whichever is more permissive."
CC0_ONLY = "This document is licensed under CC0-1.0-Universal."


def pep_text(number=9001, title="A Test Proposal", author="Ada Lovelace <ada@example.org>", copyright=PUBLIC_DOMAIN):
    return (
        f"PEP: {number}\n"
        f"Title: {title}\n"
        f"Author: {author}\n"
        "Status: Draft\n"
        "Type: Standards Track\n"
        "Created: 01-Jan-2026\n"
        "Python-Version: 3.15\n"
        "\n"
        "Abstract\n"
        "========\n"
        "\n"
        "This PEP proposes a test.\n"
        "\n"
        "Copyright\n"
        "=========\n"
        "\n"
        f"{copyright}\n"
    )


def make_checkout(root, peps):
    """A git repository laid out like github.com/python/peps, holding the given {filename: text}."""
    (root / "peps").mkdir(parents=True)
    for name, text in peps.items():
        (root / "peps" / name).write_text(text, encoding="utf-8")
    git = ["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "peps"], check=True)
    return root


# --- the header ---------------------------------------------------------------------------


def test_parse_header_reads_fields_and_continuation_lines():
    text = "PEP: 8\nTitle: Style Guide\nAuthor: Guido van Rossum,\n        Barry Warsaw\n\nBody starts here.\n"
    fields, body_start = parse_header(text)
    assert fields == {"PEP": "8", "Title": "Style Guide", "Author": "Guido van Rossum, Barry Warsaw"}
    assert body_start == 6
    assert text.splitlines()[body_start - 1] == "Body starts here."


def test_a_file_without_a_header_is_all_body():
    assert parse_header("Just a paragraph of text.\n\nAnother one.\n") == ({}, 1)


def test_a_file_that_is_only_a_header_has_no_body():
    assert parse_header("PEP: 1\nTitle: x") == ({"PEP": "1", "Title": "x"}, 3)


# --- what may be included -------------------------------------------------------------------


@pytest.mark.parametrize("statement", [
    PUBLIC_DOMAIN, CC0, CC0_ONLY,
    "This document has been PLACED IN THE PUBLIC DOMAIN.",
    "This document has been placed into the public domain.",   # PEP 427's wording
    "This document is in the public domain.",                  # PEP 215's
    "This document has been placed in the\npublic domain.",   # wrapped
])
def test_public_domain_and_cc0_are_permitted(statement):
    assert is_permitted(pep_text(copyright=statement))


def test_only_the_copyright_section_counts():
    elsewhere = pep_text(copyright="Copyright 2004. All rights reserved.").replace(
        "This PEP proposes a test.", "The reference implementation is in the public domain.")
    assert not is_permitted(elsewhere)
    no_section = "PEP: 9001\nTitle: x\n\nAbstract\n========\n\nThis document has been placed in the public domain.\n"
    assert copyright_section(no_section) == "" and not is_permitted(no_section)


def test_the_copyright_section_ends_at_the_next_section():
    text = pep_text(copyright="Copyright 2004. All rights reserved.") + (
        "\nAppendix\n========\n\nThis appendix has been placed in the public domain.\n")
    assert copyright_section(text).strip() == "Copyright 2004. All rights reserved."
    assert not is_permitted(text)


def test_an_overlined_copyright_title_is_found():
    """PEPs 258 and 517 overline their titles and indent them inside the adornment."""
    text = "PEP: 1\nTitle: x\n\n===========\n Copyright\n===========\n\n" + PUBLIC_DOMAIN + "\n"
    assert is_permitted(text)


@pytest.mark.parametrize("statement", [
    "Copyright 2004 by Example Corp. All rights reserved.",
    "This document is licensed under the Open Publication License.",
    "",
])
def test_anything_else_is_left_out(statement, tmp_path):
    assert not is_permitted(pep_text(copyright=statement))
    path = tmp_path / "pep-9001.rst"
    path.write_text(pep_text(copyright=statement), encoding="utf-8")
    assert read_pep(path, "peps/pep-9001.rst") is None


# --- email addresses ------------------------------------------------------------------------


@pytest.mark.parametrize(("authors", "expected"), [
    ("Guido van Rossum <guido@python.org>", "Guido van Rossum"),
    ("Guido van Rossum <guido at python.org>, Barry Warsaw <barry at python.org>", "Guido van Rossum, Barry Warsaw"),
    ("barry@python.org (Barry Warsaw)", "Barry Warsaw"),
    ("Brett Cannon, Paul Ganssle <paul at ganssle.io>, Pradyun Gedam", "Brett Cannon, Paul Ganssle, Pradyun Gedam"),
    ("Sébastien Eustace", "Sébastien Eustace"),
])
def test_without_emails_keeps_the_names(authors, expected):
    assert without_emails(authors) == expected


def test_redacting_the_header_keeps_every_line_and_leaves_the_body_alone():
    text = (
        "PEP: 9002\n"
        "Title: Addresses\n"
        "Author: Ada Lovelace <ada@example.org>,\n"
        "        <only-an-address at example.org>\n"
        "Discussions-To: some-list@python.org\n"
        "\n"
        "Write to python-dev@python.org or ``git clone git@github.com:python/peps``.\n"
    )
    redacted = redact_header(text)
    assert len(redacted.splitlines()) == len(text.splitlines())
    assert "example.org" not in redacted and "some-list" not in redacted
    # the continuation line that held only an address didn't become blank, which would end the header early
    assert parse_header(redacted)[1] == parse_header(text)[1] == 7
    assert parse_header(redacted)[0]["Author"] == "Ada Lovelace, (address removed)"
    assert redacted.splitlines()[-1] == text.splitlines()[-1]


def test_read_pep_parses_a_permitted_pep_and_drops_the_address(tmp_path):
    path = tmp_path / "pep-9001.rst"
    path.write_text(pep_text(), encoding="utf-8")
    pep = read_pep(path, "peps/pep-9001.rst")
    assert (pep.number, pep.title, pep.status, pep.type, pep.python_version) == (
        9001, "A Test Proposal", "Draft", "Standards Track", "3.15")
    assert pep.authors == "Ada Lovelace"
    assert "ada@example.org" not in pep.text
    assert (pep.key, pep.url) == ("pep-9001", "https://peps.python.org/pep-9001/")


def test_read_pep_rejects_a_file_that_isnt_a_pep(tmp_path):
    path = tmp_path / "pep-9001.rst"
    path.write_text(pep_text().replace("Title: A Test Proposal\n", ""), encoding="utf-8")
    with pytest.raises(CorpusError, match="doesn't look like a PEP"):
        read_pep(path, "peps/pep-9001.rst")


def test_read_pep_refuses_a_huge_file(tmp_path, monkeypatch):
    monkeypatch.setattr("evfind.peps.MAX_FILE_BYTES", 100)
    path = tmp_path / "pep-9001.rst"
    path.write_text(pep_text(), encoding="utf-8")
    with pytest.raises(CorpusError, match="larger than 100 bytes"):
        read_pep(path, "peps/pep-9001.rst")


# --- the snapshot ---------------------------------------------------------------------------


@pytest.fixture
def checkout(tmp_path):
    return make_checkout(tmp_path / "peps-repo", {
        "pep-9001.rst": pep_text(9001, "First"),
        "pep-9002.rst": pep_text(9002, "Second", author="Grace Hopper <grace at example.org>", copyright=CC0),
        "pep-9003.rst": pep_text(9003, "Not Free", copyright="Copyright 2004. All rights reserved."),
        "README.rst": "not a PEP\n",
    })


def test_build_snapshot_includes_only_permitted_peps(checkout, tmp_path):
    out = tmp_path / "snap.jsonl.gz"
    header = build_snapshot(checkout, out)
    commit = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True,
                            check=True).stdout.strip()
    assert header["commit"] == commit
    assert (header["files_seen"], header["included"]) == (3, 2)
    read_header, peps = read_snapshot(out)
    assert read_header == header
    assert [(p.number, p.title, p.authors) for p in peps] == [(9001, "First", "Ada Lovelace"),
                                                             (9002, "Second", "Grace Hopper")]
    assert "example.org" not in gzip.decompress(out.read_bytes()).decode()


def test_rebuilding_a_snapshot_gives_identical_bytes(checkout, tmp_path, monkeypatch):
    import time

    build_snapshot(checkout, tmp_path / "a.jsonl.gz")
    later = time.time() + 86_400
    monkeypatch.setattr(time, "time", lambda: later)  # a day later, and to a different file name
    build_snapshot(checkout, tmp_path / "b.jsonl.gz")
    assert (tmp_path / "a.jsonl.gz").read_bytes() == (tmp_path / "b.jsonl.gz").read_bytes()
    assert not list(tmp_path.glob("*.tmp"))


def test_build_snapshot_needs_pep_files(tmp_path):
    (tmp_path / "peps").mkdir()
    with pytest.raises(CorpusError, match="no PEP files"):
        build_snapshot(tmp_path, tmp_path / "out.jsonl.gz")


def test_build_snapshot_needs_a_git_checkout(tmp_path):
    (tmp_path / "peps").mkdir()
    (tmp_path / "peps" / "pep-9001.rst").write_text(pep_text(), encoding="utf-8")
    with pytest.raises(CorpusError, match="isn't a git checkout"):
        build_snapshot(tmp_path, tmp_path / "out.jsonl.gz")


@pytest.mark.parametrize("content", [b"not gzip at all", gzip.compress(b"not json\n"),
                                     gzip.compress(b'{"type": "something else"}\n'), gzip.compress(b"")])
def test_read_snapshot_rejects_a_file_that_isnt_one(tmp_path, content):
    path = tmp_path / "bad.jsonl.gz"
    path.write_bytes(content)
    with pytest.raises(CorpusError):
        read_snapshot(path)


def test_read_snapshot_reports_a_missing_file(tmp_path):
    with pytest.raises(CorpusError, match="isn't a readable snapshot"):
        read_snapshot(tmp_path / "missing.jsonl.gz")


# --- the bundled snapshot -------------------------------------------------------------------


@pytest.fixture(scope="module")
def bundled():
    return read_snapshot(DEFAULT_SNAPSHOT)


def test_the_bundled_snapshot_is_the_recorded_commit(bundled):
    header, peps = bundled
    assert header["commit"] == "d94bba4a5dff241fd418f30223d3bf05de27a771"
    assert (header["files_seen"], header["included"], len(peps)) == (746, 712, 712)
    assert len({p.number for p in peps}) == 712


def test_every_bundled_pep_is_permitted_and_has_no_header_address(bundled):
    for pep in bundled[1]:
        assert is_permitted(pep.text), pep.key
        header_lines = pep.text.splitlines()[: parse_header(pep.text)[1] - 1]
        assert not any(BARE_ADDRESS.search(line) or BRACKETED_ADDRESS.search(line) for line in header_lines), pep.key
        assert not BARE_ADDRESS.search(pep.authors) and "<" not in pep.authors, pep.key


def test_the_bundled_snapshot_records_its_metadata(bundled):
    pep = next(p for p in bundled[1] if p.number == 572)
    assert pep.title == "Assignment Expressions"
    assert pep.authors == "Chris Angelico, Tim Peters, Guido van Rossum"
    assert pep.source == "peps/pep-0572.rst"


def test_snapshot_lines_are_valid_json_with_the_expected_fields():
    with gzip.open(DEFAULT_SNAPSHOT, "rt", encoding="utf-8") as handle:
        handle.readline()
        first = json.loads(handle.readline())
    assert set(first) == {"number", "title", "status", "type", "created", "authors", "python_version", "source", "text"}
