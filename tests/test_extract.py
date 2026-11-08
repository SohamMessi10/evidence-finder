import re

import pytest
from docutils.nodes import make_id
from hypothesis import given
from hypothesis import strategies as st

from evfind.extract import (
    MAX_PASSAGE_WORDS,
    TRANSITION,
    PASSAGE_WORDS,
    extract_file,
    is_title,
    segment_markdown,
    segment_plain,
    segment_rst,
)
from evfind.ingest import DEFAULT_SNAPSHOT
from evfind.peps import parse_header, read_snapshot

RST = """\
=====
Title
=====

Abstract
========

First paragraph of the abstract,
over two lines.

Second paragraph.

``walrus`` and Other Things
---------------------------

Text under a subsection.
"""


def test_segment_rst_finds_sections_anchors_and_lines():
    passages = segment_rst(RST)
    assert [(p.section, p.anchor, p.line_start, p.line_end) for p in passages] == [
        ("Abstract", "abstract", 8, 11),
        ("``walrus`` and Other Things", "walrus-and-other-things", 16, 16),
    ]
    assert passages[0].text == "First paragraph of the abstract,\nover two lines.\n\nSecond paragraph."
    assert [p.ordinal for p in passages] == [0, 1]


def test_a_repeated_title_gets_no_anchor_after_the_first():
    text = "Implementation\n==============\n\nOne.\n\nOther\n=====\n\nTwo.\n\nImplementation\n==============\n\nThree.\n"
    assert [(p.section, p.anchor) for p in segment_rst(text)] == [
        ("Implementation", "implementation"), ("Other", "other"), ("Implementation", "")]


def test_pep_603_anchors_match_the_pep_site():
    """Checked against https://peps.python.org/603 (2026-11-07): the first "Implementation"
    section is #implementation, the second is an auto-numbered #id9."""
    _, peps = read_snapshot(DEFAULT_SNAPSHOT)
    pep = next(p for p in peps if p.number == 603)
    anchors = [p.anchor for p in segment_rst(pep.text, parse_header(pep.text)[1]) if p.section == "Implementation"]
    assert anchors[0] == "implementation" and set(anchors[1:]) == {""} and len(anchors) >= 2


def test_pep_572_anchors_match_the_pep_site():
    """Checked against https://peps.python.org/pep-0572/ (2026-11-07), including titles with markup."""
    _, peps = read_snapshot(DEFAULT_SNAPSHOT)
    pep = next(p for p in peps if p.number == 572)
    anchors = {p.section: p.anchor for p in segment_rst(pep.text, parse_header(pep.text)[1])}
    assert anchors["Relative precedence of ``:=``"] == "relative-precedence-of"
    assert anchors["Appendix A: Tim Peters's findings"] == "appendix-a-tim-peters-s-findings"
    assert anchors["_pydecimal.py"] == "pydecimal-py"
    assert anchors["Exceptional cases"] == "exceptional-cases"
    assert anchors["Abstract"] == "abstract"


def test_segment_rst_skips_the_header_lines():
    text = "PEP: 1\nTitle: x\n\nIntro text.\n"
    passages = segment_rst(text, body_start=parse_header(text)[1])
    assert [(p.text, p.line_start) for p in passages] == [("Intro text.", 4)]


def test_an_underline_shorter_than_its_title_isnt_a_title():
    passages = segment_rst("A long line of text\n===\n\nmore\n")
    assert passages[0].section == ""
    assert passages[0].text.startswith("A long line of text")


def test_an_indented_line_above_an_adornment_isnt_a_title():
    passages = segment_rst("Section\n=======\n\n  indented\n  --------\n")
    assert {p.section for p in passages} == {"Section"}


@pytest.mark.parametrize(("title", "underline"), [("R", "-"), ("Go", "--"), ("uv", "~~"), ("IRIX", "////"), ("Use", "!!!")])
def test_short_titles_and_any_punctuation_can_underline(title, underline):
    """PEP 450 has "R" over "-", PEP 695 "Go" over "--", PEP 3108 "IRIX" over "////"."""
    passages = segment_rst(f"Intro.\n\n{title}\n{underline}\n\nBody text.\n")
    assert [(p.section, p.text) for p in passages] == [("", "Intro."), (title, "Body text.")]


def test_an_overlined_title_may_be_indented_but_an_underlined_one_may_not():
    passages = segment_rst("===========\n Copyright\n===========\n\nPublic domain.\n")
    assert [(p.section, p.anchor, p.line_start) for p in passages] == [("Copyright", "copyright", 5)]
    # an overline of a different character doesn't make an indented line a title
    assert segment_rst("~~~~~~~~~~~\n Copyright\n===========\n\nx\n")[0].section == ""


def test_a_short_overline_stays_with_its_title():
    """An overline belongs to the title below it, not to the passage before it, even when it is
    too short to be dropped as a transition."""
    passages = segment_rst("Intro.\n\n===\nGo\n===\n\nBody.\n")
    assert [(p.section, p.text) for p in passages] == [("", "Intro."), ("Go", "Body.")]


def test_a_transition_is_dropped_but_short_punctuation_is_kept():
    passages = segment_rst("One.\n\n----------\n\n---\n\nTwo.\n")
    assert [p.text for p in passages] == ["One.\n\n---\n\nTwo."]


def test_a_title_with_a_footnote_reference_gets_no_anchor():
    """PEP 512's "cpython repo [#cpython-repo]_": the site's id for it couldn't be checked."""
    passages = segment_rst("cpython repo [#cpython-repo]_\n=============================\n\nText.\n")
    assert (passages[0].section, passages[0].anchor) == ("cpython repo [#cpython-repo]_", "")


def test_segment_markdown_uses_headings():
    text = "Intro line.\n\n# Getting Started\n\nInstall it.\n\n## Next steps ##\n\nRun it.\n"
    passages = segment_markdown(text)
    assert [(p.section, p.anchor, p.text, p.line_start) for p in passages] == [
        ("", "", "Intro line.", 1),
        ("Getting Started", "getting-started", "Install it.", 5),
        ("Next steps", "next-steps", "Run it.", 9),
    ]


@pytest.mark.parametrize("fence", ["```", "~~~~", "```python"])
def test_a_comment_in_a_markdown_code_block_isnt_a_heading(fence):
    closing = fence.rstrip("python")
    text = f"# Usage\n\n{fence}\n# set up the library\nevfind ingest --peps\n{closing}\n\nThen search.\n"
    passages = segment_markdown(text)
    assert {p.section for p in passages} == {"Usage"}
    assert "# set up the library" in passages[0].text


def test_a_markdown_heading_after_a_closed_code_block_counts():
    text = "```\n# not a heading\n```\n\n# Real heading\n\nText.\n"
    assert [p.section for p in segment_markdown(text)] == ["", "Real heading"]


def test_a_shorter_or_different_fence_doesnt_close_a_code_block():
    text = "````\n```\n# still code\n~~~~\n````\n\n# Heading\n\nText.\n"
    assert [p.section for p in segment_markdown(text)] == ["", "Heading"]


def test_segment_plain_groups_short_paragraphs_up_to_the_target():
    paragraph = " ".join(["word"] * 50)
    passages = segment_plain("\n\n".join([paragraph] * 10))
    assert [p.words for p in passages] == [200, 200, 100]  # 4 paragraphs reach 180; then 4; then 2
    assert (passages[0].line_start, passages[0].line_end, passages[1].line_start) == (1, 7, 9)


def test_a_passage_stops_growing_at_exactly_the_target():
    paragraph = " ".join(["word"] * (PASSAGE_WORDS // 2))
    assert [p.words for p in segment_plain("\n\n".join([paragraph] * 3))] == [PASSAGE_WORDS, PASSAGE_WORDS // 2]


def test_a_passage_may_reach_exactly_the_limit():
    """150 words, then a 250-word paragraph: 400 is allowed, so they stay one passage."""
    text = " ".join(["a"] * 150) + "\n\n" + " ".join(["b"] * 250)
    assert [p.words for p in segment_plain(text)] == [MAX_PASSAGE_WORDS]
    text = " ".join(["a"] * 150) + "\n\n" + " ".join(["b"] * 251)
    assert [p.words for p in segment_plain(text)] == [150, 251]


def test_a_long_code_listing_is_split_by_lines():
    lines = [f"line {i} " + " ".join(["x"] * 9) for i in range(1, 101)]  # 100 lines of 11 words
    passages = segment_plain("\n".join(lines))
    assert all(p.words <= MAX_PASSAGE_WORDS for p in passages)
    assert sum(p.words for p in passages) == 1100
    assert [(p.line_start, p.line_end) for p in passages] == [(1, 36), (37, 72), (73, 100)]


def test_a_single_line_longer_than_the_limit_is_split_by_words():
    line = " ".join(f"w{i}" for i in range(1000))
    passages = segment_markdown(f"# Notes\n\n{line}\n")
    assert [p.words for p in passages] == [400, 400, 200]
    assert {(p.line_start, p.line_end, p.section) for p in passages} == {(3, 3, "Notes")}
    assert " ".join(p.text for p in passages) == line


# --- properties ----------------------------------------------------------------------------

paragraph = st.lists(st.integers(1, 120), min_size=1, max_size=6).map(
    lambda sizes: "\n".join(" ".join(["w"] * n) for n in sizes))
document = st.lists(st.tuples(paragraph, st.integers(1, 3)), max_size=25)


def as_text(doc):
    return "".join(par + "\n" * gap for par, gap in doc)


@given(document)
def test_plain_passages_keep_every_word_in_order_within_the_limits(doc):
    text = as_text(doc)
    passages = segment_plain(text)
    assert [w for p in passages for w in p.text.split()] == text.split()
    assert all(1 <= p.words <= MAX_PASSAGE_WORDS for p in passages)
    # only the last passage may be short of the target without a reason: a paragraph that would overflow the limit
    lines = text.splitlines()
    for before, after in zip(passages, passages[1:]):
        assert before.line_end < after.line_start
        assert before.words >= PASSAGE_WORDS or before.words + after_first_paragraph(after, lines) > MAX_PASSAGE_WORDS
    assert [p.ordinal for p in passages] == list(range(len(passages)))


def after_first_paragraph(passage, lines):
    """Words in the first paragraph of a passage, read from the source lines."""
    words = 0
    for line in lines[passage.line_start - 1:]:
        if not line.strip():
            break
        words += len(line.split())
    return min(words, MAX_PASSAGE_WORDS)


# --- every PEP ------------------------------------------------------------------------------


def normalised(title):
    return re.sub(r"[^0-9a-z]+", "", FOOTNOTE.sub("", title).lower())


FOOTNOTE = re.compile(r"\[[^\]]+\]_")


def test_every_bundled_pep_has_the_sections_docutils_finds():
    """The title rule against docutils' own reStructuredText parser, on every bundled PEP: the
    same section titles, in the same order. (About 15 seconds: it parses every PEP.)"""
    from docutils import nodes
    from docutils.core import publish_doctree

    _, peps = read_snapshot(DEFAULT_SNAPSHOT)
    sections = 0
    for pep in peps:
        lines = pep.text.splitlines()
        body_start = parse_header(pep.text)[1]
        ours = [normalised(lines[i].strip()) for i in range(body_start - 1, len(lines)) if is_title(lines, i)]
        tree = publish_doctree("\n".join(lines[body_start - 1:]), settings_overrides={
            "report_level": 5, "halt_level": 5, "doctitle_xform": False,
            "file_insertion_enabled": False, "raw_enabled": False})
        theirs = [normalised(section[0].astext()) for section in tree.findall(nodes.section)]
        # docutils renders a footnote reference as its number, so compare titles with those removed
        theirs = [t.rstrip("0123456789") if o != t and o == t.rstrip("0123456789") else t
                  for o, t in zip(ours, theirs)] + theirs[len(ours):]
        assert ours == theirs, pep.key
        sections += len(ours)
    assert sections > 14_000


def test_every_pep_passage_points_at_its_exact_source_lines():
    """For every bundled PEP: each passage's lines are, in order, the lines its reference names, and
    every body line ends up in a passage unless it is a section title or an adornment."""
    _, peps = read_snapshot(DEFAULT_SNAPSHOT)
    total = 0
    for pep in peps:
        lines = pep.text.splitlines()
        _, body_start = parse_header(pep.text)
        passages = segment_rst(pep.text, body_start)
        assert passages, pep.key
        covered = set()
        previous_end = body_start - 1
        titles = [i for i in range(body_start - 1, len(lines)) if is_title(lines, i)]
        for p in passages:
            assert 1 <= p.words <= MAX_PASSAGE_WORDS, (pep.key, p.ordinal)
            assert previous_end < p.line_start <= p.line_end <= len(lines), (pep.key, p.ordinal)
            # the anchor is the title's id, unless an earlier title had the same id
            before = [i for i in titles if i + 1 < p.line_start]
            if before:
                ids_before = [make_id(lines[i].strip()) for i in before]
                assert lines[before[-1]].strip() == p.section
                repeated = ids_before[-1] in ids_before[:-1] or FOOTNOTE.search(p.section)
                expected = "" if repeated else ids_before[-1]
                assert p.anchor == expected, (pep.key, p.ordinal, p.section)
            else:
                assert (p.section, p.anchor) == ("", "")
            own = [line for line in p.text.splitlines() if line.strip()]
            assert own[0] == lines[p.line_start - 1].rstrip() and own[-1] == lines[p.line_end - 1].rstrip()
            number = p.line_start
            for line in own:  # each passage line is the next matching source line within the range
                while lines[number - 1].rstrip() != line:
                    number += 1
                    assert number <= p.line_end, (pep.key, p.ordinal, line)
                covered.add(number)
                number += 1
            previous_end = p.line_end
        for i in range(body_start - 1, len(lines)):
            if lines[i].strip() and i + 1 not in covered:
                is_adornment = (i > 0 and is_title(lines, i - 1)) or (i + 1 < len(lines) and is_title(lines, i + 1))
                assert TRANSITION.match(lines[i]) or is_title(lines, i) or is_adornment, (pep.key, i + 1, lines[i])
        total += len(passages)
    assert total > 10_000


# --- files ----------------------------------------------------------------------------------


def write(tmp_path, name, content):
    path = tmp_path / name
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.parametrize(("name", "section"), [("notes.md", "Heading"), ("notes.rst", ""), ("notes.txt", "")])
def test_extract_file_dispatches_by_suffix(tmp_path, name, section):
    result = extract_file(write(tmp_path, name, "# Heading\n\nSome text.\n"))
    assert not result.unusable and not result.problems
    assert result.passages[-1].section == section


def test_extract_file_reads_rst_sections(tmp_path):
    result = extract_file(write(tmp_path, "doc.rst", RST))
    assert [p.section for p in result.passages] == ["Abstract", "``walrus`` and Other Things"]


def test_extract_file_accepts_a_byte_order_mark(tmp_path):
    result = extract_file(write(tmp_path, "bom.txt", "﻿Hello there.".encode()))
    assert result.passages[0].text == "Hello there."


@pytest.mark.parametrize(("name", "content", "why"), [
    ("report.docx", b"PK\x03\x04", ".docx isn't supported"),
    ("README", "text", "files without an extension isn't supported"),
    ("latin1.txt", "café".encode("latin-1"), "isn't UTF-8"),
    ("empty.txt", "", "has no text"),
    ("blank.md", "\n   \n\t\n", "has no text"),
])
def test_extract_file_explains_what_it_cant_use(tmp_path, name, content, why):
    result = extract_file(write(tmp_path, name, content))
    assert why in result.unusable
    assert result.passages == []


def test_extract_file_reports_a_missing_file(tmp_path):
    assert "can't be read" in extract_file(tmp_path / "gone.txt").unusable


def test_extract_file_refuses_a_huge_file(tmp_path, monkeypatch):
    monkeypatch.setattr("evfind.extract.MAX_FILE_BYTES", 10)
    assert "the limit is 10" in extract_file(write(tmp_path, "big.txt", "x" * 11)).unusable
    assert not extract_file(write(tmp_path, "just.txt", "x" * 10)).unusable  # the limit itself is allowed
