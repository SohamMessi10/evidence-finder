# Evidence Finder

Search a document collection and get back ranked **passages**, each with an exact reference to
its source (document, section or page, line numbers, a link), so every result can be checked.
Retrieval quality is measured on a labelled question set, so any claim that one search method
beats another comes with numbers.

**Status: milestone 1 of 6 (corpus and extraction).** Search arrives in milestone 2. The plan is
in [`docs/design.md`](docs/design.md); progress is in [`PROGRESS.md`](PROGRESS.md).

## Try it

Python 3.12–3.14.

```
python -m pip install -e ".[dev]"
python -m pytest
evfind ingest --peps
evfind stats
```

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
