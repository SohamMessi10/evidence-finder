"""The PEP corpus: which PEPs may be included, and the snapshot they are stored in.

A PEP file starts with a header block of `Field: value` lines (continued on indented lines),
then a blank line, then reStructuredText. The repository has no overall license, so a PEP is
included only if its own Copyright section places it in the public domain or under CC0.

Email addresses in the header (authors, sponsors, delegates, discussion lists) are removed,
in place, so every line keeps its number; names are kept. Addresses inside the PEP's text,
mostly mailing lists and example.com samples, are left as written: a passage has to match its
source exactly for its reference to be checkable.

The snapshot is gzipped JSON Lines: a first line describing where it came from (the commit, the
rule, the counts), then one line per PEP with its header fields and its full text.
"""

from __future__ import annotations

import gzip
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from evfind.extract import is_title

# Read only in the PEP's Copyright section. The wordings found at d94bba4: "has been placed in
# the public domain", "placed into the public domain", "is in the public domain", and CC0.
PERMISSION = re.compile(
    r"placed\s+in(?:to)?\s+the\s+public\s+domain|\bis\s+in\s+the\s+public\s+domain|CC0-1\.0-Universal",
    re.IGNORECASE)
# "<guido@python.org>" and the form the PEP site uses, "<guido at python.org>"; then bare addresses.
BRACKETED_ADDRESS = re.compile(r"\s*<[^<>\n]*(?:@|\sat\s)[^<>\n]*>")
BARE_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PEP_FILE = re.compile(r"pep-(\d{4})\.(rst|txt)$")
MAX_FILE_BYTES = 2_000_000


class CorpusError(ValueError):
    """The PEP checkout or snapshot can't be used."""


@dataclass(frozen=True)
class Pep:
    number: int
    title: str
    status: str
    type: str
    created: str
    authors: str            # names only
    python_version: str
    source: str             # the file's path inside the PEP repository
    text: str               # the whole file, so line numbers match the original

    @property
    def key(self) -> str:
        return f"pep-{self.number:04d}"

    @property
    def url(self) -> str:
        return f"https://peps.python.org/pep-{self.number:04d}/"


def parse_header(text: str) -> tuple[dict[str, str], int]:
    """The header fields, and the line number (1-based) where the body starts."""
    fields: dict[str, str] = {}
    current = None
    lines = text.splitlines()
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            return fields, number + 1
        if line[:1].isspace() and current:
            fields[current] += " " + line.strip()
        elif ":" in line:
            current, _, value = line.partition(":")
            current = current.strip()
            fields[current] = value.strip()
        else:
            return {}, 1  # not a header line: there is no header, and the body is everything
    return fields, len(lines) + 1


def copyright_section(text: str) -> str:
    """The text of the section titled "Copyright", up to the next section; "" if there is none."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().lower() == "copyright" and is_title(lines, i):
            body = []
            for j in range(i + 2, len(lines)):
                if is_title(lines, j):
                    break
                body.append(lines[j])
            return "\n".join(body)
    return ""


def is_permitted(text: str) -> bool:
    """Whether the PEP's own Copyright section puts it in the public domain or under CC0."""
    return bool(PERMISSION.search(copyright_section(text)))


def without_emails(value: str) -> str:
    value = BRACKETED_ADDRESS.sub("", value)
    # the old style, "barry@python.org (Barry Warsaw)": keep the name, without its parentheses
    value = re.sub(BARE_ADDRESS.pattern + r"\s*\(([^()]*)\)", r"\1", value)
    value = BARE_ADDRESS.sub("", value)
    value = re.sub(r"\(\s*\)", "", value)          # "(barry@python.org)" leaves "()"
    value = re.sub(r"\s+,", ",", value)
    return re.sub(r"[ \t]{2,}", " ", value).strip()


def redact_header(text: str) -> str:
    """The PEP text with every email address in its header removed, keeping every line."""
    _, body_start = parse_header(text)
    lines = text.splitlines(keepends=True)
    for i in range(min(body_start - 1, len(lines))):
        content = lines[i].rstrip("\r\n")
        ending = lines[i][len(content):]
        if not (BRACKETED_ADDRESS.search(content) or BARE_ADDRESS.search(content)):
            continue
        indent = content[: len(content) - len(content.lstrip())]
        cleaned = without_emails(content)
        # A continuation line holding only an address must not become blank: a blank line ends the header.
        lines[i] = indent + (cleaned or "(address removed)") + ending
    return "".join(lines)


def read_pep(path: Path, source: str) -> Pep | None:
    """The PEP in this file, or None if it doesn't say it may be shared freely."""
    if path.stat().st_size > MAX_FILE_BYTES:
        raise CorpusError(f"{source} is larger than {MAX_FILE_BYTES} bytes")
    text = path.read_text(encoding="utf-8")
    if not is_permitted(text):
        return None
    text = redact_header(text)
    fields, _ = parse_header(text)
    match = PEP_FILE.search(path.name)
    if not match or "Title" not in fields:
        raise CorpusError(f"{source} doesn't look like a PEP (no number in its name or no Title)")
    return Pep(
        number=int(match.group(1)), title=fields["Title"], status=fields.get("Status", ""),
        type=fields.get("Type", ""), created=fields.get("Created", ""),
        authors=fields.get("Author", ""), python_version=fields.get("Python-Version", ""),
        source=source, text=text,
    )


def build_snapshot(checkout: Path, out: Path) -> dict:
    """Read every PEP in a checkout of github.com/python/peps and write the permitted ones."""
    folder = checkout / "peps"
    files = sorted(p for p in folder.glob("pep-*") if PEP_FILE.search(p.name))
    if not files:
        raise CorpusError(f"no PEP files in {folder}; is {checkout} a checkout of github.com/python/peps?")
    try:
        commit = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True,
                                text=True, check=True, timeout=30).stdout.strip()
        date = subprocess.run(["git", "-C", str(checkout), "log", "-1", "--format=%cs"], capture_output=True,
                              text=True, check=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise CorpusError(f"{checkout} isn't a git checkout, so the snapshot couldn't record its commit") from None
    peps = [pep for f in files if (pep := read_pep(f, f"peps/{f.name}"))]
    header = {
        "type": "snapshot", "source": "https://github.com/python/peps", "commit": commit, "commit_date": date,
        "rule": "included only if the PEP's Copyright section puts it in the public domain or under CC0-1.0-Universal",
        "files_seen": len(files), "included": len(peps), "emails": "removed from the header of each PEP; addresses inside the text are left as written",
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    # The same PEPs always give a byte-identical file, so a rebuild can be compared: gzip
    # otherwise records the time (mtime=0 stops that) and the file's name (an empty filename does).
    with open(tmp, "wb") as handle, gzip.GzipFile(filename="", mode="wb", fileobj=handle, mtime=0) as raw:
        raw.write((json.dumps(header) + "\n").encode())
        for pep in peps:
            raw.write((json.dumps(asdict(pep)) + "\n").encode())
    tmp.replace(out)
    return header


def read_snapshot(path: Path) -> tuple[dict, list[Pep]]:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            lines = [json.loads(line) for line in handle if line.strip()]
    except (OSError, ValueError) as exc:
        raise CorpusError(f"{path} isn't a readable snapshot ({exc})") from None
    if not lines or lines[0].get("type") != "snapshot":
        raise CorpusError(f"{path} isn't a PEP snapshot")
    return lines[0], [Pep(**line) for line in lines[1:]]
