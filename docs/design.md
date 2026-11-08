# Evidence Finder: design

## Problem

Searching a document collection is easy; finding the *evidence*, the exact passage, with a
reference someone else can check, is harder, and knowing whether a search method is actually
good is harder still. Most "chat with your documents" demos skip both: they show an answer, not
where it came from, and they never measure whether retrieval found the right passages.

Evidence Finder searches a small research collection and returns ranked passages, each with an
exact path back to its source (document, section or page, line numbers, a link). It measures
retrieval quality on a labelled question set before and after adding semantic search, so any
claim that one method is better comes with numbers.

**User:** researchers and students working with a collection of technical documents.
**Value:** answers you can verify, and an honest comparison of keyword and semantic search.

## Corpus

The demo collection is the **Python Enhancement Proposals (PEPs)**: the design documents behind
Python's features. They are real technical writing with questions that have clear answers
("which PEP added assignment expressions?"), which makes retrieval measurable.

- **Permitted:** the PEP repository has no overall license, so each PEP is included only if its
  own **Copyright section** says it is in the public domain ("placed in", "placed into", or "is
  in the public domain") or under CC0-1.0-Universal. Reading only that section stops a sentence
  elsewhere ("the reference implementation is in the public domain") from counting. At commit
  `d94bba4` (2026-11-06) that is **712 of 746 PEPs**. Of the 34 left out, 28 have no Copyright
  section or an empty one, and 6 state other terms (the Open Publication License for 3, a
  Microsoft copyright for 2, and PEP 401, an April Fools' joke). The commit and the rule are recorded in the
  snapshot.
- **Privacy:** PEP headers list email addresses for authors, sponsors and delegates, written as
  `<name@host>` or as `<name at host>`. The snapshot removes every address from each header, in
  place so every line keeps its number, and keeps the names. Addresses *inside* a PEP's text
  (242 at this commit, mostly mailing lists such as python-dev@python.org, example.com samples
  and `git@github.com` in commands) are left as written: a passage has to match its source
  exactly for its reference to be checkable.
- **Reproducible:** `evfind build-snapshot CHECKOUT -o FILE` rebuilds the snapshot from a
  checkout of that commit, byte for byte (gzip's timestamp and file name are fixed). The
  snapshot ships inside the package (`src/evfind/data/peps.jsonl.gz`, 4.5 MB), so searches
  and evaluations don't need the network.

Users can also import their own `.txt`, `.md`, `.rst` and text-based `.pdf` files.

## Passages and source references

Search returns passages, not whole documents.

| Source | Passage boundaries | Reference kept |
|---|---|---|
| PEP (reStructuredText) | sections, then paragraphs grouped to about 180 words, never more than 400 | PEP number, title, section, line range, `https://peps.python.org/pep-NNNN/#section-anchor` |
| `.txt`, `.md`, `.rst` | headings where present (a `#` line inside a Markdown code block isn't one), then paragraphs | file, section, line range |
| `.pdf` | each page, then paragraphs | file, page number (no line numbers: extracted line breaks needn't match the page's) |

Section titles are found by docutils' rule (an underline at least as long as the title, in any
ASCII punctuation; an optional matching overline, which allows an indented title), and a test
checks that it finds the same sections as docutils' own parser in every bundled PEP.

Section anchors are docutils' `make_id` of the section title, which is the id the PEP site
uses; checked against peps.python.org for six sections of PEP 572, including titles with markup
(the title "Relative precedence of :=", with `:=` marked as code, is `#relative-precedence-of`). When a title repeats within a
PEP, docutils gives the later section an auto-numbered id that depends on the whole document
(PEP 603's second "Implementation" is `#id9`), so those sections get no anchor and link to the
PEP's page instead: never to the wrong section. Titles holding a footnote reference get none
either, since their id couldn't be checked. That is 272 of the 17,165 PEP passages.

**Extraction failures are reported, not skipped silently.** A PDF with no extractable text on
any page (scanned images, or genuinely empty) is flagged as needing OCR, which isn't supported;
pages without text, or that fail to extract, inside an otherwise readable PDF are listed. A PDF
encrypted only against copying or printing (an empty open password) is read; one that needs a
password is refused, as are damaged files, each with the reason. Documents that can't be used
are still recorded in the library, with why, so `evfind stats` shows them.

## Architecture

```
 PEP snapshot (data/*.jsonl.gz) ─┐
 your .txt / .md / .rst / .pdf ──┼─► extract.py: passages + source references + problems
                                 ▼
                         store.py: SQLite (documents, passages, problems)
                                 │
                 ┌───────────────┼──────────────────┐
                 ▼               ▼                  ▼
          keyword: FTS5     semantic: LSA      hybrid: rank fusion       (milestones 2, 4)
                 └───────────────┼──────────────────┘
                                 ▼
               evaluation: Recall@k, MRR, nDCG on held-out questions     (milestone 3)
                                 ▼
               CLI and a search page: ranked excerpts → source links     (milestones 2, 5)
```

## Tech stack

| Part | Choice | Why |
|---|---|---|
| Language | Python 3.12–3.14 | consistent with the other projects |
| Storage and keyword search | SQLite FTS5 (BM25 ranking) | built into Python, fast, well tested; no server |
| reStructuredText anchors | docutils 0.23 | its `make_id` is the rule the PEP site uses |
| PDF text | pypdf 6.21 | pure Python, reports encryption and per-page text |
| Semantic search | scikit-learn LSA (TF-IDF + truncated SVD) | runs offline; the build machine can't download neural embedding models, so this is labelled as LSA, not neural embeddings |
| Interface | command line, then a FastAPI page | the stack from SplitLedger |
| Tests | pytest, Hypothesis; fpdf2 to generate test PDFs | no third-party PDFs in the repo |

## Evaluation (milestone 3)

A labelled set of questions about the PEPs, each with the PEPs that answer it, split before any
tuning into a **tuning** set (used to choose settings) and a **test** set (used once, for the
reported numbers). Metrics: Recall@5 and @10 (was a right document found?), MRR (how high was
the first right one?), nDCG@10. The corpus size, question counts, and how labels were made are
reported with every result.

## Security

Local tool. Imported files are read, never executed; file size and passage counts are capped;
the search page (milestone 5) listens on 127.0.0.1 only and escapes everything it shows.

## Milestones

1. Corpus snapshot and extraction with source references and failure reporting.
2. Keyword search (FTS5, BM25) with highlighted excerpts; `evfind search`.
3. Labelled evaluation set (tuning and test), metrics, `evfind eval`.
4. Semantic (LSA) and hybrid search, compared on the test set.
5. Search page: ranked excerpts with links to the exact source.
6. README, methodology, resume and interview material.
