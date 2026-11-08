"""Command line: `evfind ingest`, `evfind stats`, and `evfind build-snapshot`.

Exit codes: 0 success; 1 something couldn't be done (a missing library, an unreadable
snapshot), or every file given to `ingest` was unusable; 2 wrong command usage.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from evfind import __version__
from evfind.peps import CorpusError
from evfind.store import Library, StoreError

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2
DEFAULT_LIBRARY = Path("evfind.db")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evfind", description="Find passages, with exact sources, in a document collection.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

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
