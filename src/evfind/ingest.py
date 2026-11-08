"""Importing documents into a library: the PEP snapshot, or your own files."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from evfind.extract import Extracted, extract_file, segment_rst
from evfind.peps import parse_header, read_snapshot
from evfind.store import Library

KINDS = {".txt": "text", ".md": "markdown", ".rst": "rst", ".pdf": "pdf"}
# Shipped inside the package, so it is there however the package was installed.
DEFAULT_SNAPSHOT = Path(__file__).resolve().parent / "data" / "peps.jsonl.gz"


@dataclass
class IngestReport:
    indexed: list[str] = field(default_factory=list)
    unusable: list[tuple[str, str]] = field(default_factory=list)      # (source, why)
    warnings: list[tuple[str, str]] = field(default_factory=list)      # (source, what)
    passages: int = 0


def ingest_peps(library: Library, snapshot: Path = DEFAULT_SNAPSHOT) -> tuple[IngestReport, dict]:
    header, peps = read_snapshot(snapshot)
    report = IngestReport()
    for pep in peps:
        _, body_start = parse_header(pep.text)
        extracted = Extracted(passages=segment_rst(pep.text, body_start))
        meta = {"number": pep.number, "status": pep.status, "type": pep.type, "created": pep.created,
                "authors": pep.authors, "python_version": pep.python_version,
                "snapshot_commit": header["commit"], "path_in_repo": pep.source}
        library.put(pep.key, "pep", f"PEP {pep.number}: {pep.title}", pep.source, extracted, url=pep.url, meta=meta)
        report.indexed.append(pep.key)
        report.passages += len(extracted.passages)
    return report, header


def ingest_files(library: Library, paths: list[Path]) -> IngestReport:
    report = IngestReport()
    for path in paths:
        source = str(path)
        extracted = extract_file(path)
        kind = KINDS.get(path.suffix.lower(), "text")
        title = path.stem.replace("_", " ").replace("-", " ").strip() or path.name
        library.put(f"file:{path.resolve()}", kind, title, source, extracted)
        if extracted.unusable:
            report.unusable.append((source, extracted.unusable))
        else:
            report.indexed.append(source)
            report.passages += len(extracted.passages)
        report.warnings += [(source, problem) for problem in extracted.problems]
    return report
