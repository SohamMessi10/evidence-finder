# Progress

## Status

**Milestone 4 of 6 complete: semantic and hybrid search, measured.** Next: a search page
(FastAPI) showing ranked excerpts with links to the exact source. The full plan is in
[`docs/design.md`](docs/design.md); the evaluation is in [`docs/evaluation.md`](docs/evaluation.md).

## Milestones

### Milestone 4: semantic and hybrid search

- `semantic.py`: LSA (scikit-learn 1.9 TF-IDF and truncated SVD, seeded) over each passage's
  title, section and text; cosine similarity. Named LSA, not embeddings: the build machine
  can't download neural models.
- The index is stored in the library (schema 3, table `semantic_index`) as numpy arrays loaded
  with `allow_pickle=False`, with a fingerprint of the passages and settings. It is fitted on
  the first semantic search (2.9 s for the 17,165 PEP passages at 100 dimensions) and refitted
  when the library changes.
- Hybrid search: weighted reciprocal rank fusion of the keyword and LSA document rankings.
  `evfind search --method keyword|semantic|hybrid`; `evfind eval --method … [--dimensions N]`;
  `evfind tune --method hybrid`.
- **Tuning questions only:** hybrid at 100 dimensions, keyword weight 2, *c* = 10 (MRR 0.788;
  400 and 500 dimensions tied at 0.787). A prototype had preferred 400, but it fused only the
  top 100 of each ranking; the shipped tuning, which fuses exactly as search does, decided.
  - LSA alone was best at 800 dimensions (0.680), and was tested there.
  - Keyword search stayed the default (0.797 on tuning), fixed before the test run.
- **Test questions, run once:**
  - *Hybrid against keyword:* Recall@10 0.915 against 0.852, paired +0.062 [+0.010, +0.115];
    questions with nothing relevant in the top 10 fell from 13 to 7, all of them paraphrased.
    MRR +0.026 [−0.014, +0.068], not clear.
  - *LSA alone:* no better than keyword search on any metric.
- Recorded rather than done: making hybrid the default. These test results favour it, but
  choosing the default from them would turn the test set into a tuning set.
- Found while building:
  - The dimension count was frozen into `fit`'s signature, so changing it didn't reach the
    index; it is now read when called.
  - Libraries opened in the new tests weren't closed (the warning filter caught it).
  - On a 12-passage test library, 100 dimensions reproduce the word overlap exactly, so LSA
    can't generalise; those tests use 2.

**Tests (247, about 2 min; passing on Python 3.12, 3.13 and 3.14):**
- LSA links related words across two topics ("automobile" finds the truck passage).
- Deterministic fitting, a byte round trip, refusal of pickled or older-format arrays, and too
  small or all-stop-word libraries.
- The index is built once, stored, reused, and rebuilt when the library changes or the stored
  copy is unreadable.
- Fusion arithmetic and ties; all three methods in both result modes.
- The schema 2 → 3 upgrade, and the commands.
- The published numbers: keyword exactly; hybrid and LSA within 0.02, since the SVD's
  floating-point sums can differ between machines.

**Mutation-checked:** 24 deliberate bugs. 18 were caught at once. Of the 6 survivors:
- 4 were test gaps, each now with a test:
  - the guard for fewer than 2 dimensions;
  - hybrid's choice of a document's keyword passage;
  - tuning ignoring the dimensions given;
  - `--compare` comparing hybrid with itself.
- 1 was equivalent, and the code was simplified so the condition is gone.
- 1 is equivalent in practice: numpy's default sort happens to keep tied similarities in order,
  but isn't documented to, so the explicit tie-break stays.

### Milestone 3: labelled evaluation

- 145 questions about the PEPs, each with the PEP or PEPs that answer it, written by Claude and
  checked against the corpus by title before any search was run on them. 89 use the PEP's own
  terms; 56 are paraphrased. One was dropped (its answer, PEP 202, isn't in the corpus) and two
  labels removed on re-reading, all before use.
- Split once by a seeded shuffle within each kind: 49 tuning questions, 96 test questions.
  `evfind tune` refuses test questions.
- `evaluate.py`: Recall@5, Recall@10, MRR and nDCG@10 over documents, with 95% percentile
  bootstrap intervals (fixed seed) and paired comparisons. `evfind eval [--split]
  [--compare] [--misses]` and `evfind tune`.
- **Tuning:** of 18 column-weight settings, title 8 / section 1 / text 1 did best on the tuning
  questions: MRR 0.797 against 0.721 for equal weights, paired +0.076 [+0.025, +0.135]. The grid
  was extended from 8 to 32 before choosing, because the first best was at its edge.
- **Found while tuning:** FTS5 normalises BM25 by the whole row's length, so a heavy title
  weight made each document's shortest passage its "best", showing PEP 577 as its one-line
  copyright notice. Documents are now ranked by the tuned weights, and the passage shown for
  each is chosen by its section and text alone. The labels are per document, so that choice is
  a design decision rather than a measured one.
- **Test results, run once:** MRR 0.708 [0.631, 0.782], Recall@10 0.852 [0.781, 0.917].
  - *Terms questions:* MRR 0.894; every labelled PEP is in the top 10.
  - *Paraphrased questions:* MRR 0.411; 62% of the labelled PEPs are in the top 10.
  - *Misses:* all 13 questions with no labelled PEP in the top 10 are paraphrased.
- **The tuned weights didn't clearly carry over:** +0.030 MRR on the test questions, 95% interval
  [−0.008, +0.072], which includes zero. This is reported as it is; no claim is made that the
  tuning helped.
- Not done on purpose: no labels were added after seeing test results (some misses are fair
  related PEPs), and search wasn't changed after the test run. Dropping common words like "how"
  and "do" (a question matches 15,186 of 17,165 passages on average) is a candidate for
  milestone 4, chosen on tuning questions.

**Tests (213, about 70 s; passing on Python 3.12, 3.13 and 3.14):** the metrics against values worked out by hand, and Hypothesis
properties (every metric between 0 and 1; Recall@5 ≤ Recall@10). They also cover:
- the bootstrap's reproducibility;
- the bundled questions: counts, splits, and every label naming a PEP in the corpus;
- bad question files;
- tuning refusing test questions;
- a test that pins the published test-set numbers, so the docs can't drift from the code;
- the `eval` and `tune` commands.

**Mutation-checked:** 24 deliberate bugs in the metrics, the bootstrap, tuning, the new
passage choice and the commands. 20 were caught at once. The 4 survivors were gaps in the tests,
not bugs in the code, and each now has a test:
- the ideal nDCG not capped at 10 (no question has more than 10 labels);
- the bootstrap's percentiles replaced by its extremes, and an unseeded bootstrap (the old test
  used 8 values, too few for the percentiles to move);
- the `--misses` filter (the test didn't check the count).

### Milestone 2: keyword search

- The library's schema is now version 2. It adds a search index: an SQLite FTS5 table over each
  passage's document title, section and text, with Porter stemming ("expressions" finds
  "expression") and accents ignored ("cafe" finds "café"). Triggers keep it in step with the
  passages, including rows removed by `ON DELETE CASCADE` (checked first: SQLite fires delete
  triggers for those). A milestone 1 library is upgraded the first time it is opened, in one
  transaction, so a failed upgrade leaves it as it was.
- `search.py`: the query is split into words and each word is quoted, so nothing typed can be
  read as FTS5 syntax. Raw text fails: `what is := ?` gives "no such column: is". The words are
  joined with OR and ranked by BM25, with equal weights for title, section and text for now.
  Milestone 3 chooses the weights, using the tuning questions only.
- Results are passages, by default the best one from each document, with how many more matched
  there. Each result has an excerpt with the matched words marked, and a link to the PEP's
  section when its anchor is known. It also links to the passage's exact lines in the PEP
  repository at the snapshot's commit (`…/blob/d94bba4…/peps/pep-0572.rst?plain=1#L17-L28`).
  GitHub documents `?plain=1` for Markdown; for reStructuredText it is IMPLEMENTED, and to be
  confirmed by opening one link.
- `evfind search WORDS... [--limit N] [--all-passages]`: bold highlights in a terminal (none
  with `NO_COLOR`), `**word**` when piped. Exit codes: 0 with or without matches, 1 for a
  missing library, 2 for a query with no words or a bad option.
- Measured on the 2-CPU build workspace, 712 PEPs and 17,165 passages:
  - *Search:* 300 queries of 1–6 words drawn from PEP titles took 18.0 ms at the median and
    40.8–45.7 ms at the 95th percentile, 60 ms at most (two runs). Merging the index with
    FTS5's `optimize` saved a millisecond or two and enlarged the file, so it isn't done.
  - *Import:* `evfind ingest --peps` now takes 4.6–4.9 s (3 runs; 2.1–2.3 s without the
    index), and the library is 43 MB (17.5 MB without it).
- Noticed, and left for milestone 3 to measure rather than adjusting by eye:
  - "walrus operator" ranks PEP 622 (with its section "Walrus patterns") above PEP 572.
  - "assignment expressions" ranks PEP 379, the withdrawn first proposal, just above PEP 572.
  - BM25 favours very short passages: PEP 572's best match there is one line, a code line
    matched only through its section title. 2,173 of the 17,165 passages (12.7%) are under 20
    words; some are one-sentence sections, others the short tail of a longer section. Merging
    short tails is a candidate to test on the tuning questions.
  - Natural-language questions are dominated by common words ("what", "is").
- Limits: punctuation and operators can't be searched (`:=` is ignored). Chinese, Japanese and
  Korean text isn't split into words.

**Tests (183, about 45–50 s; passing on Python 3.12, 3.13 and 3.14):**
- Any typed text, including FTS5 operators, quotes and `NEAR(`, gives a valid expression or
  "nothing to search for" (Hypothesis, 300 cases).
- Any sequence of imports and re-imports leaves the index holding exactly the passages, with
  their current title, section and text (Hypothesis).
- Triggers follow updates and deletions; the version 1 upgrade, including a failed one; a SQLite
  without FTS5.
- Ranking, grouping, ties, excerpts, links and places, and bad settings.
- On the PEPs, a functional check (not a quality measure) that title words find their PEP, and
  that every PEP result links to its exact lines.
- The command: its output, `--all-passages`, no matches, a query of punctuation only, limits.

**Mutation-checked:** 31 deliberate bugs, each run against the whole suite.
- 27 were caught at once.
- Three survivors exposed real gaps, and each now has a test that catches it:
  - excerpts taken from whichever column matched best instead of the passage text;
  - `remove_diacritics 1` instead of 2, which "café" can't tell apart but "Tiếng Việt" can;
  - `--all-passages` dropped from the search call, which the test missed because it checked
    only the heading.
- One is equivalent in practice: without the passage-id tie-break, SQLite still returns equal
  scores in id order. SQL doesn't promise that, so the tie-break stays.

### Milestone 1: corpus and extraction

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
