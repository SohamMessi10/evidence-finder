"""Turn documents into passages, each with an exact reference back to where it came from.

A passage is a few paragraphs from one section (or one PDF page), about PASSAGE_WORDS words,
never more than MAX_PASSAGE_WORDS. Search returns passages, so every result can be checked:
the reference says which document, which section or page, and which lines.

Extraction problems are returned, not swallowed. A PDF whose pages hold no text is almost
always a scan: it is flagged as needing OCR, which isn't supported, instead of being indexed
as an empty document that silently never matches anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from docutils.nodes import make_id

PASSAGE_WORDS = 180
MAX_PASSAGE_WORDS = 400
MAX_FILE_BYTES = 20_000_000
MAX_PDF_PAGES = 2000

# reStructuredText adornment: a line of one repeated ASCII punctuation character, any of
# !"#$%&'()*+,-./:;<=>?@[\]^_`{|}~ (PEP 3108 underlines with "////", PEP 450 titles "R" with "-").
ADORNMENT = re.compile(r"^([!-/:-@\[-`{-~])\1*\s*$")
# A transition (a horizontal rule) is an adornment line of four or more characters on its own.
TRANSITION = re.compile(r"^([!-/:-@\[-`{-~])\1{3,}\s*$")
# A footnote or citation reference in a title, "[#name]_" or "[1]_", changes the id the PEP site gives it.
FOOTNOTE_REFERENCE = re.compile(r"\[[^\]]+\]_")
MARKDOWN_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
# A fenced code block opens and closes with ``` or ~~~ (CommonMark); "# comment" inside one isn't a heading.
MARKDOWN_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


@dataclass(frozen=True)
class Passage:
    ordinal: int            # position in the document, from 0
    text: str
    section: str = ""       # the section heading it falls under, if any
    anchor: str = ""        # the section's id in the rendered document, if known
    page: int | None = None  # PDF page, from 1
    line_start: int | None = None  # lines in the source file, from 1, inclusive
    line_end: int | None = None

    @property
    def words(self) -> int:
        return len(self.text.split())


@dataclass
class Extracted:
    passages: list[Passage] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)   # things the reader should know
    unusable: str = ""                                  # why nothing could be indexed, if so


@dataclass(frozen=True)
class _Block:
    """A paragraph: lines between blank lines, with where they were."""

    lines: list[str]
    start: int
    end: int
    section: str
    anchor: str
    page: int | None = None


def _paragraphs(lines: list[str], first_line: int, section: str, anchor: str, page: int | None = None) -> list[_Block]:
    blocks, current, start = [], [], None
    for number, line in enumerate(lines, start=first_line):
        if line.strip():
            if not current:
                start = number
            current.append(line.rstrip())
        elif current:
            blocks.append(_Block(current, start, number - 1, section, anchor, page))
            current = []
    if current:
        blocks.append(_Block(current, start, first_line + len(lines) - 1, section, anchor, page))
    return blocks


def _split_long(block: _Block) -> list[_Block]:
    """A paragraph longer than MAX_PASSAGE_WORDS (a long code listing, say), split by lines.

    A single line that is itself too long (a whole paragraph written on one line, common in
    Markdown) is split by words; every piece keeps that line's number.
    """
    if sum(len(line.split()) for line in block.lines) <= MAX_PASSAGE_WORDS:
        return [block]
    pieces: list[tuple[int, str, int]] = []  # (line number, text, words)
    for number, line in enumerate(block.lines, start=block.start):
        words = line.split()
        if len(words) <= MAX_PASSAGE_WORDS:
            pieces.append((number, line, len(words)))
        else:
            pieces += [(number, " ".join(words[i:i + MAX_PASSAGE_WORDS]), len(words[i:i + MAX_PASSAGE_WORDS]))
                       for i in range(0, len(words), MAX_PASSAGE_WORDS)]
    out: list[_Block] = []
    current: list[tuple[int, str, int]] = []
    for piece in pieces:
        if current and sum(p[2] for p in current) + piece[2] > MAX_PASSAGE_WORDS:
            out.append(_Block([p[1] for p in current], current[0][0], current[-1][0],
                              block.section, block.anchor, block.page))
            current = []
        current.append(piece)
    out.append(_Block([p[1] for p in current], current[0][0], current[-1][0], block.section, block.anchor, block.page))
    return out


def _group(blocks: list[_Block]) -> list[Passage]:
    """Join consecutive paragraphs of the same section (and page) into passages of about PASSAGE_WORDS."""
    passages: list[Passage] = []
    group: list[_Block] = []

    def flush():
        if group:
            text = "\n\n".join("\n".join(b.lines) for b in group)
            passages.append(Passage(len(passages), text, group[0].section, group[0].anchor, group[0].page,
                                    group[0].start, group[-1].end))
            group.clear()

    words = 0
    for block in (piece for b in blocks for piece in _split_long(b)):
        n = sum(len(line.split()) for line in block.lines)
        same_place = group and (block.section, block.page) == (group[0].section, group[0].page)
        if group and (not same_place or words >= PASSAGE_WORDS or words + n > MAX_PASSAGE_WORDS):
            flush()
            words = 0
        group.append(block)
        words += n
    flush()
    return passages


def _overlined(lines: list[str], i: int) -> bool:
    prev = lines[i - 1] if i > 0 else ""
    nxt = lines[i + 1] if i + 1 < len(lines) else ""
    return bool(ADORNMENT.match(prev) and ADORNMENT.match(nxt) and prev.strip()[0] == nxt.strip()[0])


def is_title(lines: list[str], i: int) -> bool:
    """Whether line i is a reStructuredText section title, by docutils' rule.

    A title is underlined by an adornment at least as long as it, and may also be overlined
    with the same character; only an overlined title may be indented (inset). Checked against
    docutils' own parser: the same sections in every bundled PEP (tests/test_extract.py).
    """
    line = lines[i]
    nxt = lines[i + 1] if i + 1 < len(lines) else ""
    if not line.strip() or ADORNMENT.match(line) or not ADORNMENT.match(nxt):
        return False
    if len(nxt.rstrip()) < len(line.rstrip()):
        return False
    return _overlined(lines, i) or not line.startswith((" ", "\t"))


def segment_rst(text: str, body_start: int = 1) -> list[Passage]:
    """Passages from reStructuredText, by section. Lines before `body_start` (a PEP header) are skipped.

    A section's anchor is docutils' `make_id` of its title, which is the id the PEP site gives
    it. When a title's id is already taken by an earlier section, docutils gives the later one
    an auto-numbered id ("id9") that depends on the whole document, so that section gets no
    anchor: a link to the page is better than a link to the wrong section. The same goes for a
    title holding a footnote reference, whose id couldn't be checked.
    """
    lines = text.splitlines()
    blocks: list[_Block] = []
    section, anchor = "", ""
    used: set[str] = set()
    i = body_start - 1
    chunk_start = i

    def close_chunk(end_index):
        blocks.extend(_paragraphs(lines[chunk_start:end_index], chunk_start + 1, section, anchor))

    while i < len(lines):
        if is_title(lines, i):
            close_chunk(i - 1 if _overlined(lines, i) else i)
            section = lines[i].strip()
            candidate = make_id(section)
            anchor = "" if candidate in used or FOOTNOTE_REFERENCE.search(section) else candidate
            used.add(candidate)
            i += 2
            chunk_start = i
            continue
        i += 1
    close_chunk(len(lines))
    return _group([b for b in blocks if not all(TRANSITION.match(x) for x in b.lines)])


def segment_markdown(text: str) -> list[Passage]:
    lines = text.splitlines()
    blocks, section, start = [], "", 0
    fence = ""  # the marker that opened the code block we are in, if any
    for i, line in enumerate(lines):
        fenced = MARKDOWN_FENCE.match(line)
        if fenced:
            marker = fenced.group(1)
            if not fence:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence) and not line[fenced.end():].strip():
                fence = ""
            continue
        match = None if fence else MARKDOWN_HEADING.match(line)
        if match:
            blocks += _paragraphs(lines[start:i], start + 1, section, make_id(section) if section else "")
            section, start = match.group(2), i + 1
    blocks += _paragraphs(lines[start:], start + 1, section, make_id(section) if section else "")
    return _group(blocks)


def segment_plain(text: str) -> list[Passage]:
    return _group(_paragraphs(text.splitlines(), 1, "", ""))


def extract_pdf(path: Path) -> Extracted:
    """Passages from each page of a text-based PDF, and everything that went wrong."""
    from pypdf import PasswordType, PdfReader
    from pypdf.errors import DependencyError, PyPdfError

    result = Extracted()
    # pypdf raises its own errors, and also these from damaged or unusual files (found by fuzzing:
    # an unsupported encryption handler or compression filter is NotImplementedError).
    page_errors = (PyPdfError, ValueError, KeyError, TypeError, AttributeError, IndexError, NotImplementedError)
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            # Many PDFs are encrypted only to restrict printing or copying, with an empty
            # password for opening them; those can be read. A PDF that needs a password can't.
            try:
                opened = reader.decrypt("")
            except DependencyError:
                result.unusable = ("the PDF is AES-encrypted; reading it needs the optional `cryptography` "
                                   "package (pip install cryptography)")
                return result
            if opened == PasswordType.NOT_DECRYPTED:
                result.unusable = "the PDF needs a password to open, and password-protected PDFs aren't supported"
                return result
        pages = reader.pages
        count = len(pages)
    except (*page_errors, OSError) as exc:
        result.unusable = f"the PDF can't be read ({type(exc).__name__}: {exc})"[:300]
        return result
    if count == 0:
        result.unusable = "the PDF has no pages"
        return result
    if count > MAX_PDF_PAGES:
        result.unusable = f"the PDF has {count} pages; the limit is {MAX_PDF_PAGES}"
        return result

    blocks, empty, broken = [], [], []
    for number in range(1, count + 1):
        try:
            text = (pages[number - 1].extract_text() or "").strip()
        except page_errors:
            broken.append(number)
            continue
        if not text:
            empty.append(number)
            continue
        blocks += _paragraphs(text.splitlines(), 1, "", "", page=number)

    if broken and len(broken) == count:
        result.unusable = "no page could be read: the PDF is probably damaged"
    elif len(empty) + len(broken) == count:
        result.unusable = ("no page has extractable text: it is probably a scanned image, "
                           "and reading scans (OCR) isn't supported")
    else:
        # The reference for a PDF passage is its page. Line breaks in extracted PDF text needn't
        # match the lines on the page, so no line numbers are claimed.
        result.passages = [replace(p, line_start=None, line_end=None) for p in _group(blocks)]
        if empty:
            result.problems.append(f"{len(empty)} of {count} pages have no extractable text (pages "
                                   f"{_pages(empty)}); they may be scanned images and weren't indexed")
        if broken:
            result.problems.append(f"{len(broken)} of {count} pages couldn't be read (pages {_pages(broken)}) "
                                   f"and weren't indexed")
    return result


def _pages(numbers: list[int]) -> str:
    return ", ".join(map(str, numbers[:10])) + (" and more" if len(numbers) > 10 else "")


def extract_file(path: Path) -> Extracted:
    """Passages from a .txt, .md, .rst or .pdf file."""
    result = Extracted()
    try:
        size = path.stat().st_size
    except OSError as exc:
        result.unusable = f"can't be read ({exc.strerror})"
        return result
    if size > MAX_FILE_BYTES:
        result.unusable = f"the file is {size:,} bytes; the limit is {MAX_FILE_BYTES:,}"
        return result
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf(path)
    if suffix not in (".txt", ".md", ".rst"):
        result.unusable = f"{suffix or 'files without an extension'} isn't supported; use .txt, .md, .rst or .pdf"
        return result
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        result.unusable = "the file isn't UTF-8 text"
        return result
    segment = {".md": segment_markdown, ".rst": segment_rst}.get(suffix, segment_plain)
    result.passages = segment(text)
    if not result.passages:
        result.unusable = "the file has no text"
    return result
