# Evidence Finder

Search a document collection and get back ranked **passages**, each with an exact reference to
its source (document, section or page, line numbers, a link), so every result can be checked.
Retrieval quality is measured on a labelled question set, so any claim that one search method
beats another comes with numbers.

**Status: milestone 3 of 6 (measured search quality).** Semantic search comes next. The plan
is in [`docs/design.md`](docs/design.md), the evaluation in
[`docs/evaluation.md`](docs/evaluation.md), and progress in [`PROGRESS.md`](PROGRESS.md).

## How good is it?

Measured once on 96 held-out test questions about the PEPs (95% bootstrap intervals):

| | Questions using the PEP's terms (59) | Paraphrased questions (37) |
|---|---|---|
| Recall@10 | 1.000 | 0.617 [0.468, 0.766] |
| MRR | 0.894 [0.824, 0.952] | 0.411 [0.292, 0.548] |

Keyword search finds the answer when the question shares its words, and often misses it when
it doesn't. That gap is what semantic search, the next milestone, has to close. The questions
were written and labelled by an AI assistant before any search was run on them, not by
independent judges; [`docs/evaluation.md`](docs/evaluation.md) explains how they were made,
what was tuned, and what the numbers can't show.

## Try it

Python 3.12–3.14.

```
python -m pip install -e ".[dev]"
python -m pytest
evfind ingest --peps
evfind search assignment expressions
```

```
1,651 passages in 364 documents match. The best passage from each of the top 2 documents:

  1. PEP 572: Assignment Expressions
     Exceptional cases · lines 149–181
     https://peps.python.org/pep-0572/#exceptional-cases
     source: https://github.com/python/peps/blob/d94bba4a5dff241fd418f30223d3bf05de27a771/peps/pep-0572.rst?plain=1#L149-L181
     There are a few places where **assignment** **expressions** are not allowed, in order to
     avoid ambiguities or user confusion: - Unparenthesized **assignment** **expressions** are
     prohibited at the top level of an **expression** statement…
     and 53 more matching passages in this document

  2. PEP 379: Adding an Assignment Expression
     Abstract · lines 14–18
     https://peps.python.org/pep-0379/#abstract
     source: https://github.com/python/peps/blob/d94bba4a5dff241fd418f30223d3bf05de27a771/peps/pep-0379.rst?plain=1#L14-L18
     This PEP adds a new **assignment** **expression** to the Python language to make it possible
     to **assign** the result of an **expression** in almost any place. The new **expression**
     will allow the…
     and 7 more matching passages in this document
```

Every result says where it came from: the section and lines, a link to that section, and a
link to the exact lines in the PEP repository at the snapshot's commit.

`evfind ingest --peps` builds a library (`evfind.db`) from the bundled PEPs: 712 documents,
17,165 passages. Your own files work too:

```
evfind ingest notes.md paper.pdf
```

Files that can't be used (a scanned PDF with no text, a password-protected PDF, an unsupported
format) are listed with the reason, and `evfind stats` shows them later.

## The data

The demo collection is the Python Enhancement Proposals, from
[github.com/python/peps](https://github.com/python/peps) at commit `d94bba4` (2026-11-06). That
repository has no overall license, so a PEP is included only if its own Copyright section
places it in the public domain or under CC0-1.0-Universal: 712 of 746. Email addresses are
removed from every PEP's header. Details: [`docs/design.md`](docs/design.md#corpus).

The code is under the MIT license ([`LICENSE`](LICENSE)). The bundled PEP text
(`src/evfind/data/peps.jsonl.gz`) is not covered by it: it keeps the terms each PEP states, the
public domain or CC0.
