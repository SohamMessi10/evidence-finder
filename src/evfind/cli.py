"""Command line: `evfind search`, `evfind ingest`, `evfind stats`, `evfind eval`, `evfind tune`,
and `evfind build-snapshot`.

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
    find.add_argument("--method", choices=("keyword", "semantic", "hybrid"), default="keyword",
                      help="keyword (BM25, the default), semantic (LSA), or hybrid (both, fused)")
    find.add_argument("--all-passages", action="store_true",
                      help="list every matching passage, not just the best one from each document")
    find.add_argument("--library", type=Path, default=DEFAULT_LIBRARY)

    ingest = commands.add_parser("ingest", help="import the PEPs and/or your own files into a library")
    ingest.add_argument("files", nargs="*", type=Path, help=".txt, .md, .rst or .pdf files")
    ingest.add_argument("--peps", action="store_true", help="import the bundled collection of PEPs (Python Enhancement Proposals)")
    ingest.add_argument("--library", type=Path, default=DEFAULT_LIBRARY, help="the library file (default evfind.db)")

    stats = commands.add_parser("stats", help="what a library holds, and any problems found while importing")
    stats.add_argument("--library", type=Path, default=DEFAULT_LIBRARY)

    ev = commands.add_parser("eval", help="measure search quality on the labelled questions")
    ev.add_argument("--split", choices=("tuning", "test"), default="test")
    ev.add_argument("--weights", type=_weights, default=None, metavar="T,S,X",
                    help="title, section and text weights (default: the tuned ones)")
    ev.add_argument("--method", choices=("keyword", "semantic", "hybrid"), default="keyword")
    ev.add_argument("--dimensions", type=int, default=None, metavar="N",
                    help="evaluate semantic or hybrid search with an LSA index of N dimensions, fitted now")
    ev.add_argument("--compare", action="store_true",
                    help="also show the paired difference from keyword search (from equal weights, for keyword)")
    ev.add_argument("--misses", action="store_true", help="list questions with no relevant document in the top 10")
    ev.add_argument("--questions", type=Path, default=None, help="a questions file (default: the bundled one)")
    ev.add_argument("--library", type=Path, default=DEFAULT_LIBRARY)

    tn = commands.add_parser("tune", help="try settings on the tuning questions (never the test ones)")
    tn.add_argument("--method", choices=("keyword", "hybrid"), default="keyword",
                    help="keyword: BM25 column weights; hybrid: LSA dimensions and fusion (takes minutes)")
    tn.add_argument("--questions", type=Path, default=None)
    tn.add_argument("--library", type=Path, default=DEFAULT_LIBRARY)

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


def _weights(text: str) -> tuple[float, float, float]:
    try:
        values = tuple(float(part) for part in text.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r}: give three numbers, like 8,1,1") from None
    if len(values) != 3 or not all(v > 0 for v in values):
        raise argparse.ArgumentTypeError(f"{text!r}: give three positive numbers, like 8,1,1")
    return values


def _questions(path):
    from evfind.evaluate import DEFAULT_QUESTIONS, load_questions

    return load_questions(path or DEFAULT_QUESTIONS)


def _weights_label(weights) -> str:
    return "title {:g}, section {:g}, text {:g}".format(*weights)


def eval_command(args) -> int:
    from evfind.evaluate import KINDS, METRICS, EvaluationError, compare, run, summarise
    from evfind.search import DEFAULT_WEIGHTS, EQUAL_WEIGHTS

    weights = args.weights or DEFAULT_WEIGHTS
    try:
        questions = [q for q in _questions(args.questions) if q.split == args.split]
        with Library.open(args.library) as library:
            if library.document("pep-0001") is None:
                raise StoreError(f"{args.library} has no PEPs; the questions are about them: run evfind ingest --peps")
            options = {}
            if args.dimensions is not None:
                from evfind.semantic import SemanticIndex, SemanticUnavailable, _rows

                if args.method == "keyword" or args.dimensions < 2:
                    raise EvaluationError("--dimensions needs --method semantic or hybrid, and at least 2")
                try:
                    options["index"] = SemanticIndex.fit(_rows(library), "eval", dimensions=args.dimensions)
                except SemanticUnavailable as exc:
                    raise EvaluationError(str(exc)) from None
            outcomes = run(library, questions, weights, args.method, **options)
            if not args.compare:
                baseline = None
            elif args.method == "keyword":
                baseline, baseline_label = run(library, questions, EQUAL_WEIGHTS), f"equal weights ({_weights_label(EQUAL_WEIGHTS)})"
            else:
                baseline, baseline_label = run(library, questions, weights), "keyword search"
    except (StoreError, EvaluationError) as exc:
        print(f"evfind: {exc}", file=sys.stderr)
        return EXIT_FAILED
    kinds = {kind: [o for o in outcomes if o.question.kind == kind] for kind in KINDS}
    if args.method == "keyword":
        setting = f"keyword search, weights {_weights_label(weights)}"
    else:
        from evfind.semantic import DIMENSIONS, FUSION_C, KEYWORD_WEIGHT

        setting = f"{args.method} search, {args.dimensions or DIMENSIONS} LSA dimensions"
        if args.method == "hybrid":
            setting += f", keyword weight {KEYWORD_WEIGHT:g}, c {FUSION_C:g}"
    print(f"{_count(len(questions), args.split + ' question')} ({len(kinds['terms'])} using the PEP's terms, "
          f"{len(kinds['paraphrase'])} paraphrased); {setting}")
    print("95% bootstrap intervals in brackets.\n")
    groups = [("all", outcomes)] + [(kind, group) for kind, group in kinds.items() if group]
    summaries = {name: summarise(group) for name, group in groups}
    print(f"{'':<11}" + "".join(f"{name:<22}" for name, _ in groups))
    for metric in METRICS:
        print(f"{metric:<11}" + "".join(f"{'{:.3f} [{:.3f}, {:.3f}]'.format(*summaries[name][metric]):<22}"
                                         for name, _ in groups))
    if baseline is not None:
        print(f"\nChange from {baseline_label}, paired:")
        for metric in METRICS:
            mean, low, high = compare(baseline, outcomes, metric)
            print(f"  {metric:<10} {mean:+.3f} [{low:+.3f}, {high:+.3f}]")
    if args.misses:
        missed = [o for o in outcomes if o.scores["recall@10"] == 0]
        print(f"\n{_count(len(missed), 'question')} with no relevant document in the top 10:")
        for o in missed:
            print(f"  {o.question.id} ({o.question.kind}) {o.question.text}")
            print(f"      wanted {', '.join(o.question.relevant)}; got {', '.join(o.ranked[:3]) or 'nothing'}")
    return EXIT_OK


def tune_command(args) -> int:
    from evfind.evaluate import EvaluationError, tune, tune_semantic

    try:
        questions = [q for q in _questions(args.questions) if q.split == "tuning"]
        with Library.open(args.library) as library:
            ranking = tune(library, questions) if args.method == "keyword" else tune_semantic(library, questions)
    except (StoreError, EvaluationError) as exc:
        print(f"evfind: {exc}", file=sys.stderr)
        return EXIT_FAILED
    print(f"Mean reciprocal rank on {_count(len(questions), 'tuning question')}, best first:")
    if args.method == "keyword":
        for weights, mrr in ranking:
            print(f"  {_weights_label(weights):<32} {mrr:.3f}")
    else:
        for k, method, setting, mrr in ranking:
            label = f"{method}, {k} dimensions" + (f", keyword weight {setting[0]:g}, c {setting[1]:g}" if setting else "")
            print(f"  {label:<52} {mrr:.3f}")
    return EXIT_OK


def _emphasis() -> tuple[str, str]:
    """Bold in a terminal (unless NO_COLOR is set); **like this** when the output is piped."""
    if sys.stdout.isatty() and not os.environ.get("NO_COLOR"):
        return "\x1b[1m", "\x1b[0m"
    return "**", "**"


def format_results(results: Results, *, grouped: bool, emphasis: tuple[str, str], width: int = 100) -> str:
    if not results.hits:
        return f"No passage matches {results.query!r}."
    if results.method == "semantic":
        found = (f"{_count(results.passages_matched, 'passage')} in {_count(results.documents_matched, 'document')} "
                 "are related by meaning (LSA).")
    else:
        found = None
    shown = len(results.hits)
    if grouped:
        best = ("The best passage from the top document:" if shown == 1
                else f"The best passage from each of the top {shown} documents:")
    else:
        best = "The top passage:" if shown == 1 else f"The top {shown} passages:"
    matched = f"{_count(results.passages_matched, 'passage')} in {_count(results.documents_matched, 'document')}"
    lines = [f"{found or matched + (' matches.' if results.passages_matched == 1 else ' match.')} {best}", ""]
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
            results = search(library, query, limit=args.limit, per_document=None if args.all_passages else 1,
                             method=args.method, progress=lambda message: print(message, file=sys.stderr))
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
    if args.command == "eval":
        return eval_command(args)
    if args.command == "tune":
        return tune_command(args)
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
