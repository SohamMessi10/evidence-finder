# Progress

## Status

**Milestone 1 of 6 complete: the corpus and extraction.** Next: keyword search (SQLite FTS5,
BM25) and `evfind search`. The full plan is in [`docs/design.md`](docs/design.md).

## Milestones

### Milestone 1: corpus and extraction

- Checked before building: the PEP repository has no overall license, so permission is read
  per PEP. Hugging Face is unreachable from the build machine, so semantic search (milestone 4)
  will be LSA, labelled as such. Versions: pypdf 6.21, docutils 0.23, fpdf2 2.8.9 (tests only),
  Python 3.12–3.14.
- `peps.py`: reads a checkout of github.com/python/peps and keeps a PEP only if its Copyright
  section puts it in the public domain or under CC0: **712 of 746** at commit `d94bba4`
  (2026-11-06). Removes every email address from each header, in place, so line numbers still
  match the original file. `evfind build-snapshot` writes the snapshot (gzipped JSON Lines,
  4.5 MB) and records the commit, the rule and the counts; a rebuild is byte-identical.
- `extract.py`: turns documents into passages of about 180 words (never more than 400), each
  with its section, anchor, page and line range. reStructuredText titles follow docutils' rule;
  Markdown headings; plain text by paragraphs; PDFs page by page with pypdf.
  - Failures are reported rather than skipped: no text on any page ("probably a scanned image;
    OCR isn't supported"), some pages without text (listed), a page that fails to extract, a
    password, too many pages, a damaged file, an unsupported format, not UTF-8, empty.
  - A PDF encrypted only against copying (an empty open password) is read.
- `store.py`: the library in SQLite with STRICT tables and CHECK constraints (no passage whose
  last line comes before its first, no page 0, valid JSON metadata). Importing a document again
  replaces it, in one transaction; unusable documents are kept with the reason.
- `evfind ingest --peps`, `evfind ingest FILES...`, `evfind stats`.
- Measured on the 2-CPU build workspace: the 712 PEPs give 17,165 passages (1,858,836 words;
  median 93 words per passage). Segmenting them all takes 0.6 s; `evfind ingest --peps` takes
  2.1–2.3 s end to end (3 runs) and builds a 17.5 MB library.

**Tests (140, about 35–40 s; passing on Python 3.12, 3.13 and 3.14):**
- *All PEPs:* every PEP passage's lines are, in order, the source lines its reference names,
  and every body line lands in a passage unless it is a title or an adornment. The title rule
  finds the same sections as docutils' own parser in every bundled PEP. Anchors match
  peps.python.org for the sections checked by hand.
- *Corpus:* the license filter, email removal, snapshot determinism (a day later and under
  another name).
- *Passages:* Hypothesis properties (no word lost or reordered, the size limits).
- *PDFs:* generated with fpdf2: text, blank and image-only pages, encryption with and without a
  password, damaged files.
- *Store and CLI:* constraints and the commands.
- PDF fuzzing: 300 damaged PDFs per test run; 40,000 in a one-off run, with no crash.

**Bugs found and fixed before anything was published:**
1. Email addresses: the first version removed only `<name@host>` from the author field. It
   missed the `<name at host>` form (27 PEPs), and every address stayed in the stored full text
   of each header. Now every header line is cleaned in place, and a test checks every bundled PEP.
2. The permission rule was too narrow and too loose at once: it missed "placed **into** the
   public domain" and "is in the public domain" (15 PEPs, including 427 on wheels and 428 on
   pathlib), and it searched the whole text, so a sentence anywhere could count. It now reads
   only the Copyright section.
3. Section titles: the first rule missed overlined, indented titles (PEPs 258, 517), underlines
   in characters such as `/` (PEP 3108), and titles of one or two letters (PEP 450's "R").
   Found by comparing with docutils' parser; now identical on every bundled PEP.
4. A repeated section title would have linked to the first section with that title. The PEP
   site gives the later one an auto-numbered id (`#id9` in PEP 603), so it now gets no anchor.
5. Fuzzing found that 428 of 5,000 damaged PDFs crashed extraction with `NotImplementedError`
   (an unsupported encryption handler or compression filter). Now reported as unreadable.
6. `Library.open` leaked its SQLite connection when it refused a file (caught by the test
   suite's ResourceWarning filter).
7. A snapshot written under another file name wasn't byte-identical (gzip stores the name).
8. Markdown: a `# comment` line inside a fenced code block was read as a heading.
9. `evfind stats | head -1` ended in a `BrokenPipeError` traceback. It now stops quietly, as
   Python's documentation recommends. Its test first passed for the wrong reason: the build
   machine sets `PYTHONUNBUFFERED`, so the "buffered" case wasn't buffered.
10. Smaller ones: a single line over 400 words made an oversized passage; a file with no header
   was read as all header; `barry@python.org (Barry Warsaw)` kept its parentheses; "1 passages".
11. A decision rather than a bug: PDF passages no longer claim line numbers, since line breaks in
   extracted PDF text needn't match the lines on the page. Their reference is the page.
12. Two wrong expectations in my own tests (a line number, and a phrase that wraps in the
   source), corrected after checking the source.

**Mutation-checked:** deliberate bugs, each run against the whole suite.
- *Final code, 38 bugs:* 34 of the 37 valid ones were caught at first. One mutant was invalid
  (my replacement text broke the Python syntax, so its "kill" proved nothing); it was rerun
  correctly and caught. The three survivors were:
  - an overline left in the previous passage, harmless unless the overline is shorter than a
    transition;
  - a passage reaching exactly 400 words, caught before only when Hypothesis happened to
    generate that case;
  - the `NotImplementedError` catch, which the 300-case fuzz test never exercises.
  Each now has a fixed test, and all 38 are caught.
- *An earlier round of 27, against the first version,* found four more gaps: the CC0-only
  wording, 3-character underlines, exactly 180 words, and a PDF with only blank and broken
  pages. It also found one equivalent mutant: `<=` to `<` before splitting a 400-word paragraph
  gives the same single piece.
- *Five more, for the code-block, PDF-line and pipe fixes,* are all caught, one of them only
  after its test was made genuinely buffered (fix 9 above).
