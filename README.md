# Evidence Finder

Search a document collection and get back ranked **passages**, each with an exact reference to
its source (document, section or page, line numbers, a link), so every result can be checked.
Retrieval quality is measured on a labelled question set, so any claim that one search method
beats another comes with numbers.

**Status: milestone 2 of 6 (keyword search).** Measuring search quality comes next. The plan is
in [`docs/design.md`](docs/design.md); progress is in [`PROGRESS.md`](PROGRESS.md).

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

  1. PEP 379: Adding an Assignment Expression
     Abstract · lines 14–18
     https://peps.python.org/pep-0379/#abstract
     source: https://github.com/python/peps/blob/d94bba4a5dff241fd418f30223d3bf05de27a771/peps/pep-0379.rst?plain=1#L14-L18
     This PEP adds a new **assignment** **expression** to the Python language to make it possible
     to **assign** the result of an **expression** in almost any place. The new **expression**
     will allow the…
     and 7 more matching passages in this document

  2. PEP 572: Assignment Expressions
     Differences between  assignment expressions and assignment statements · line 453
     https://peps.python.org/pep-0572/#differences-between-assignment-expressions-and-assignment-statements
     source: https://github.com/python/peps/blob/d94bba4a5dff241fd418f30223d3bf05de27a771/peps/pep-0572.rst?plain=1#L453
     total += tax # Equivalent: (total := total + tax)
     and 53 more matching passages in this document
```

Every result says where it came from: the section and lines, a link to that section, and a
link to the exact lines in the PEP repository at the snapshot's commit. The second result also
shows a weakness that milestone 3 will measure: PEP 572's best passage is a single line,
matched only through its section title, because BM25 favours very short passages.

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
