"""Command line: `evfind search`, `evfind ingest`, `evfind stats`, and `evfind build-snapshot`.

Exit codes: 0 success; 1 something couldn't be done (a missing library, an unreadable
snapshot), or every file given to `ingest` was unusable; 2 wrong command usage.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import textwrap
from pathlib import Path

from evfind import __version__
from evfind.peps import CorpusError
from evfind.search import MAX_LIMIT, Results, SearchError, render_excerpt, search
from evfind.store import Library, StoreError

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2
DEFAULT_LIBRARY = Path("evfind.db")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evfind", description="Find passages, with exact sources, in a document collection.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    find = commands.add_parser("search", help="find passages, each with exactly where it came from")
    find.add_argument("query", nargs="+", help="words to look for (punctuation and operators are ignored)")
    find.add_argument("-n", "--limit", type=_limit, default=10, metavar="N",
                      help=f"how many results to show (1 to {MAX_LIMIT}; default 10)")
    find.add_argument("--all-passages", action="store_true",
                      help="list every matching passage, not just the best one from each document")
    find.add_argument("--library", type=Path, default=DEFAULT_LIBRARY)

    ingest = commands.add_parser("ingest", help="import the PEPs and/or your own files into a library")
    ingest.add_argument("files", nargs="*", type=Path, help=".txt, .md, .rst or .pdf files")
    ingest.add_argument("--peps", action="store_true", help="import the bundled collection of PEPs (Python Enhancement Proposals)")
    ingest.add_argument("--library", type=Path, default=DEFAULT_LIBRARY, help="the library file (default evfind.db)")

    stats = commands.add_parser("stats", help="what a library holds, and any problems found while importing")
    stats.add_argument("--library", type=Path, default=DEFAULT_LIBRARY)

    snap = commands.add_parser("build-snapshot", help="rebuild the PEP snapshot from a checkout of github.com/python/peps")
    snap.add_argument("checkout", type=Path)
    snap.add_argument("-o", "--output", type=Path, required=True)
    return parser


def _limit(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} isn't a whole number") from None
    if not 1 <= value <= MAX_LIMIT:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_LIMIT}")
    return value


def _emphasis() -> tuple[str, str]:
    """Bold in a terminal (unless NO_COLOR is set); **like this** when the output is piped."""
    if sys.stdout.isatty() and not os.environ.get("NO_COLOR"):
        return "\x1b[1m", "\x1b[0m"
    return "**", "**"


def format_results(results: Results, *, grouped: bool, emphasis: tuple[str, str], width: int = 100) -> str:
    if not results.hits:
        return f"No passage matches {results.query!r}."
    shown = len(results.hits)
    if grouped:
        best = ("The best passage from the top document:" if shown == 1
                else f"The best passage from each of the top {shown} documents:")
    else:
        best = "The top passage:" if shown == 1 else f"The top {shown} passages:"
    matched = f"{_count(results.passages_matched, 'passage')} in {_count(results.documents_matched, 'document')}"
    lines = [f"{matched} {'matches' if results.passages_matched == 1 else 'match'}. {best}", ""]
    indent = " " * 5
    for hit in results.hits:
        lines.append(f"{hit.rank:>3}. {hit.title}")
        if hit.place:
            lines.append(indent + hit.place)
        lines.append(indent + (hit.url or hit.source))
        if hit.source_url:
            lines.append(indent + "source: " + hit.source_url)
        excerpt = render_excerpt(hit.excerpt, *emphasis)
        lines += textwrap.wrap(excerpt, width=width, initial_indent=indent, subsequent_indent=indent) or []
        if grouped and hit.more_in_document:
            lines.append(indent + f"and {_count(hit.more_in_document, 'more matching passage')} in this document")
        lines.append("")
    return "\n".join(lines).rstrip()


def search_command(args) -> int:
    query = " ".join(args.query)
    try:
        with Library.open(args.library) as library:
            results = search(library, query, limit=args.limit, per_document=None if args.all_passages else 1)
    except StoreError as exc:
        print(f"evfind: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except SearchError as exc:
        print(f"evfind: {exc}", file=sys.stderr)
        return EXIT_USAGE
    width = max(60, min(110, shutil.get_terminal_size((100, 24)).columns - 2))
    print(format_results(results, grouped=not args.all_passages, emphasis=_emphasis(), width=width))
    return EXIT_OK


def _count(n: int, noun: str) -> str:
    return f"{n:,} {noun}" + ("" if n == 1 else "s")


def ingest_command(args, parser) -> int:
    from evfind.ingest import ingest_files, ingest_peps

    if not args.peps and not args.files:
        parser.error("give files to import, or --peps for the PEP collection, or both")
    try:
        with Library.open(args.library, create=True) as library:
            if args.peps:
                report, header = ingest_peps(library)
                print(f"imported {_count(len(report.indexed), 'PEP')} as {_count(report.passages, 'passage')} "
                      f"(snapshot of {header['source']} at {header['commit'][:7]}, {header['commit_date']})")
            failed = False
            if args.files:
                report = ingest_files(library, args.files)
                print(f"imported {len(report.indexed)} of {_count(len(args.files), 'file')} "
                      f"as {_count(report.passages, 'passage')}")
                for source, why in report.unusable:
                    print(f"  not imported: {source}: {why}")
                for source, what in report.warnings:
                    print(f"  warning: {source}: {what}")
                failed = not report.indexed
    except (StoreError, CorpusError) as exc:
        print(f"evfind: {exc}", file=sys.stderr)
        return EXIT_FAILED
    print(f"library: {args.library}")
    return EXIT_FAILED if failed else EXIT_OK


def stats_command(args) -> int:
    try:
        with Library.open(args.library) as library:
            s = library.stats()
            rows = library.conn.execute(
                "SELECT d.source, p.detail FROM problems p JOIN documents d ON d.id = p.document_id ORDER BY d.source, p.id"
            ).fetchall()
    except StoreError as exc:
        print(f"evfind: {exc}", file=sys.stderr)
        return EXIT_FAILED
    print(f"{_count(s['documents'], 'document')} ({s['indexed']:,} indexed, {s['unusable']:,} unusable), "
          f"{_count(s['passages'], 'passage')}, {_count(s['words'], 'word')}")
    for source, detail in rows:
        print(f"  {source}: {detail}")
    return EXIT_OK


def snapshot_command(args) -> int:
    from evfind.peps import build_snapshot

    try:
        header = build_snapshot(args.checkout, args.output)
    except CorpusError as exc:
        print(f"evfind: {exc}", file=sys.stderr)
        return EXIT_FAILED
    print(f"{header['included']} of {header['files_seen']} PEPs included (commit {header['commit'][:7]}, "
          f"{header['commit_date']}) -> {args.output}")
    return EXIT_OK


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "search":
        return search_command(args)
    if args.command == "ingest":
        return ingest_command(args, parser)
    if args.command == "stats":
        return stats_command(args)
    return snapshot_command(args)


def main(argv: list[str] | None = None) -> int:
    try:
        code = run(argv)
        sys.stdout.flush()  # so a closed pipe is noticed here, not at exit
        return code
    except BrokenPipeError:
        # The reader went away (`evfind stats | head`): stop quietly, as Python's documentation
        # recommends, pointing stdout at devnull so the flush at exit doesn't fail again.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
