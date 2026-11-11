"""The search page: a small local web interface to a library (FastAPI).

`evfind serve` runs it on 127.0.0.1 only. It has no accounts and changes nothing: it reads the
library and shows ranked passages with their exact sources.

Safety: every value shown is HTML-escaped, and only then are the excerpt's match markers (control
characters that can't come from the escaped text) replaced with <mark> tags. The page sends a
Content Security Policy that allows no scripts at all, and its stylesheet comes from this server.
Each request opens its own SQLite connection, since requests run on a pool of threads.
"""

from __future__ import annotations

import threading
from html import escape
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response

from evfind import semantic
from evfind.search import (
    MARK_END,
    MARK_START,
    MAX_LIMIT,
    METHODS,
    Results,
    SearchError,
    render_excerpt,
    search,
)
from evfind.store import Library, StoreError

MAX_QUERY_CHARS = 500
METHOD_NAMES = {"keyword": "Keyword", "hybrid": "Hybrid (keyword + meaning)", "semantic": "Meaning only (LSA)"}
EXAMPLES = ["assignment expressions", "how do I make sure a file gets closed even when an error happens",
            "positional-only parameters", "structural pattern matching tutorial"]

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; form-action 'self'; "
                               "base-uri 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

STYLE = """
:root { --bg: #fbfbf9; --fg: #1d1d1b; --muted: #5f5f5a; --line: #deded8; --accent: #1f5fbf;
        --mark: #ffe58a; --card: #ffffff; --on-accent: #ffffff; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #161615; --fg: #ececea; --muted: #a3a39d; --line: #34342f; --accent: #8ab4ff;
          --mark: #6b5600; --card: #1e1e1c; --on-accent: #0b1a33; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
       font: 16px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 780px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 1.5rem; margin: 0 0 4px; }
.lede { color: var(--muted); margin: 0 0 20px; }
form { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; }
input[type=search] { flex: 1 1 320px; min-width: 0; padding: 10px 12px; font: inherit; color: var(--fg);
                     background: var(--card); border: 1px solid var(--line); border-radius: 8px; }
select, button { padding: 10px 12px; font: inherit; border-radius: 8px; border: 1px solid var(--line);
                 background: var(--card); color: var(--fg); }
button { background: var(--accent); color: var(--on-accent); border-color: var(--accent); cursor: pointer; }
:focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }
.hint { color: var(--muted); font-size: 0.875rem; margin: 0 0 24px; }
.summary { color: var(--muted); margin: 0 0 12px; }
ol.results { list-style: none; padding: 0; margin: 0; }
.result { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px;
          margin-bottom: 12px; }
.result h2 { font-size: 1.05rem; margin: 0 0 2px; }
.result h2 .rank { color: var(--muted); font-weight: normal; margin-right: 6px; }
.place { color: var(--muted); font-size: 0.875rem; margin: 0 0 8px; overflow-wrap: anywhere; }
.excerpt { margin: 0 0 8px; overflow-wrap: anywhere; }
mark { background: var(--mark); color: inherit; padding: 0 1px; border-radius: 2px; }
.links { font-size: 0.875rem; margin: 0; display: flex; flex-wrap: wrap; gap: 4px 16px; }
a { color: var(--accent); }
.more { color: var(--muted); font-size: 0.875rem; margin: 6px 0 0; }
.notice { border: 1px solid var(--line); border-left: 4px solid var(--accent); background: var(--card);
          padding: 12px 14px; border-radius: 8px; }
.notice.error { border-left-color: #c0392b; }
code { font-size: 0.9em; }
"""


def excerpt_html(excerpt: str) -> str:
    """Escape the excerpt, then turn its match markers into <mark> tags."""
    return escape(render_excerpt(excerpt, MARK_START, MARK_END)).replace(MARK_START, "<mark>").replace(MARK_END, "</mark>")


def excerpt_spans(excerpt: str) -> tuple[str, list[list[int]]]:
    """The excerpt as plain text, and the [start, end) character offsets of its matched words."""
    text, spans, start = [], [], None
    for char in render_excerpt(excerpt, MARK_START, MARK_END):
        if char == MARK_START:
            start = sum(map(len, text))
        elif char == MARK_END and start is not None:
            spans.append([start, sum(map(len, text))])
            start = None
        else:
            text.append(char)
    return "".join(text), spans


def create_app(library_path: Path) -> FastAPI:
    library_path = Path(library_path)
    app = FastAPI(title="Evidence Finder", docs_url=None, redoc_url=None, openapi_url=None)
    cache: dict[str, semantic.SemanticIndex] = {}
    lock = threading.Lock()

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    def run(query: str, method: str, limit: int, per_document: int | None) -> Results:
        with Library.open(library_path) as library:
            options = {}
            if method != "keyword":
                with lock:  # one build at a time; afterwards every request shares the index
                    current = semantic.fingerprint(library)
                    if "index" not in cache or cache["index"].fingerprint != current:
                        try:
                            cache["index"] = semantic.load_index(library)
                        except semantic.SemanticUnavailable as exc:
                            raise SearchError(str(exc)) from None
                    options["index"] = cache["index"]
            return search(library, query, limit=limit, per_document=per_document, method=method, **options)

    def stats() -> dict:
        with Library.open(library_path) as library:
            return library.stats()

    @app.get("/style.css")
    def style() -> Response:
        return Response(STYLE, media_type="text/css")

    @app.get("/health")
    def health() -> JSONResponse:
        try:
            s = stats()
        except StoreError as exc:
            return JSONResponse({"status": "unavailable", "detail": str(exc)}, status_code=503)
        return JSONResponse({"status": "ok", "documents": s["documents"], "passages": s["passages"]})

    @app.get("/api/search")
    def api_search(q: str = Query(..., min_length=1, max_length=MAX_QUERY_CHARS),
                   method: Literal["keyword", "semantic", "hybrid"] = "keyword",
                   limit: int = Query(10, ge=1, le=MAX_LIMIT), all_passages: bool = False) -> JSONResponse:
        try:
            results = run(q, method, limit, None if all_passages else 1)
        except StoreError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=503)
        except SearchError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        hits = []
        for hit in results.hits:
            text, spans = excerpt_spans(hit.excerpt)
            hits.append({"rank": hit.rank, "score": hit.score, "key": hit.key, "title": hit.title,
                         "section": hit.section, "place": hit.place, "page": hit.page,
                         "line_start": hit.line_start, "line_end": hit.line_end, "url": hit.url,
                         "source_url": hit.source_url, "source": hit.source, "excerpt": text,
                         "highlights": spans, "more_in_document": hit.more_in_document})
        return JSONResponse({"query": results.query, "method": results.method,
                             "passages_matched": results.passages_matched,
                             "documents_matched": results.documents_matched, "hits": hits})

    @app.get("/", response_class=HTMLResponse)
    def page(q: str = "", method: str = "keyword", n: str = "10") -> HTMLResponse:
        q = q.strip()
        if method not in METHODS:
            return _page(q, "keyword", _notice(f"Unknown search method {method!r}.", error=True), 400)
        try:
            limit = int(n)  # its range is checked by search(), which says the same thing
        except ValueError:
            return _page(q, method, _notice(f"The number of results must be between 1 and {MAX_LIMIT}.", error=True), 400)
        if len(q) > MAX_QUERY_CHARS:
            return _page(q[:MAX_QUERY_CHARS], method,
                         _notice(f"The query is too long: at most {MAX_QUERY_CHARS} characters.", error=True), 400)
        try:
            if not q:
                return _page(q, method, _intro(stats()))
            results = run(q, method, limit, 1)
        except StoreError as exc:
            return _page(q, method, _notice(f"{exc}.", error=True), 503)
        except SearchError as exc:
            return _page(q, method, _notice(str(exc)[:1].upper() + str(exc)[1:] + ".", error=True), 400)
        return _page(q, method, _results(results))

    return app


def _notice(text: str, *, error: bool = False) -> str:
    return f'<p class="notice{" error" if error else ""}" role="{"alert" if error else "status"}">{escape(text)}</p>'


def _intro(s: dict) -> str:
    examples = " · ".join(f'<a href="/?{urlencode({"q": e})}">{escape(e)}</a>' for e in EXAMPLES)
    return (f'<p class="summary">This library holds {s["documents"]:,} documents in {s["passages"]:,} passages. '
            f"Every result says exactly where it came from.</p><p>Try: {examples}</p>")


def _results(results: Results) -> str:
    if not results.hits:
        return _notice(f"No passage matches “{results.query}”. Try other words, or the hybrid method.")
    found = (f"{results.passages_matched:,} passages in {results.documents_matched:,} documents "
             + ("are related by meaning" if results.method == "semantic" else "match")
             + f". The best passage from each of the top {len(results.hits)}:")
    items = []
    for hit in results.hits:
        title = escape(hit.title)
        heading = f'<a href="{escape(hit.url)}" rel="noopener noreferrer">{title}</a>' if hit.url else title
        links = []
        if hit.url:
            links.append(f'<a href="{escape(hit.url)}" rel="noopener noreferrer">Read the section</a>')
        if hit.source_url:
            links.append(f'<a href="{escape(hit.source_url)}" rel="noopener noreferrer">Exact lines at the snapshot commit</a>')
        if not hit.url:
            links.append(f"<span>{escape(hit.source)}</span>")
        more = (f'<p class="more">and {hit.more_in_document:,} more matching passage'
                f'{"" if hit.more_in_document == 1 else "s"} in this document</p>') if hit.more_in_document else ""
        items.append(
            f'<li class="result"><h2><span class="rank">{hit.rank}.</span>{heading}</h2>'
            f'<p class="place">{escape(hit.place)}</p><p class="excerpt">{excerpt_html(hit.excerpt)}</p>'
            f'<p class="links">{"".join(links)}</p>{more}</li>')
    return f'<p class="summary" role="status">{escape(found)}</p><ol class="results">{"".join(items)}</ol>'


def _page(q: str, method: str, body: str, status: int = 200) -> HTMLResponse:
    options = "".join(f'<option value="{m}"{" selected" if m == method else ""}>{escape(name)}</option>'
                      for m, name in METHOD_NAMES.items())
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(q) + " · " if q else ""}Evidence Finder</title>
<link rel="stylesheet" href="/style.css">
</head>
<body>
<main>
<h1>Evidence Finder</h1>
<p class="lede">Search the collection; every result links to the exact place it came from.</p>
<form method="get" action="/" role="search">
<input type="search" name="q" value="{escape(q)}" maxlength="{MAX_QUERY_CHARS}" aria-label="Search the collection" placeholder="Ask a question or type some words" autofocus>
<select name="method" aria-label="Search method">{options}</select>
<button type="submit">Search</button>
</form>
<p class="hint">Keyword is the default. Hybrid found more of the right documents for paraphrased questions in the evaluation; the first meaning-based search builds its index, which takes a few seconds.</p>
{body}
</main>
</body>
</html>"""
    return HTMLResponse(html, status_code=status)
