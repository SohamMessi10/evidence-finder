import re

import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st

from evfind import semantic
from evfind.cli import main
from evfind.extract import Extracted, Passage
from evfind.search import MARK_END, MARK_START
from evfind.store import Library
from evfind.web import MAX_QUERY_CHARS, SECURITY_HEADERS, create_app, excerpt_html, excerpt_spans


@pytest.fixture(scope="module")
def peps(pep_library):
    with TestClient(create_app(pep_library)) as client:
        yield client


HOSTILE_TITLE = "<script>alert('title')</script> Walrus & Friends"
HOSTILE_TEXT = 'The walrus <img src=x onerror="alert(1)"> said "hello" & left. ' + " ".join(["filler"] * 20)


@pytest.fixture
def hostile(tmp_path):
    path = tmp_path / "lib.db"
    with Library.open(path, create=True) as lib:
        lib.put("file:h", "text", HOSTILE_TITLE, "/notes/<b>h</b>.txt",
                Extracted(passages=[Passage(0, HOSTILE_TEXT, "Intro <i>x</i>", "", None, 1, 3)]))
        lib.put("file:u", "text", "Linked walrus", "u.txt", Extracted(passages=[Passage(0, "walrus link")]),
                url='https://x.example/"onmouseover="alert(1)')
        for i, text in enumerate(["car engine wheels road", "automobile engine fuel road", "car automobile driver",
                                  "apple banana orchard fruit", "banana fruit market sweet", "cherry apple fruit"]):
            lib.put(f"file:{i}", "text", f"Note {i}", f"/notes/{i}.txt", Extracted(passages=[Passage(0, text)]))
    with TestClient(create_app(path)) as client:
        yield client


def no_raw_markup(html: str) -> bool:
    """No script, and no tags other than the page's own."""
    allowed = {"html", "head", "meta", "title", "link", "body", "main", "h1", "h2", "p", "form", "input", "select",
               "option", "button", "ol", "li", "a", "span", "mark"}
    tags = {t.lower() for t in re.findall(r"</?([a-zA-Z][a-zA-Z0-9]*)", html)}
    return "<script" not in html.lower() and tags <= allowed


# --- the page ----------------------------------------------------------------------------------


def test_the_front_page_introduces_the_library(peps):
    response = peps.get("/")
    assert response.status_code == 200
    assert "712 documents in 17,165 passages" in response.text
    assert 'href="/?q=assignment+expressions"' in response.text
    assert no_raw_markup(response.text)


def test_every_response_carries_the_security_headers(peps):
    for path in ("/", "/?q=walrus", "/style.css", "/health", "/api/search?q=walrus", "/?q=%3F"):
        headers = peps.get(path).headers
        for name, value in SECURITY_HEADERS.items():
            assert headers[name] == value, (path, name)
    policy = peps.get("/").headers["Content-Security-Policy"]
    assert policy.startswith("default-src 'none';") and "script-src" not in policy  # no scripts at all
    assert "frame-ancestors 'none'" in policy and "style-src 'self'" in policy


def test_a_search_shows_ranked_passages_with_their_sources(peps):
    html = peps.get("/", params={"q": "positional-only parameters"}).text
    assert html.count('class="result"') == 10
    assert "PEP 570: Python Positional-Only Parameters" in html
    assert 'href="https://peps.python.org/pep-0570/' in html
    assert 'href="https://github.com/python/peps/blob/d94bba4a5dff241fd418f30223d3bf05de27a771/peps/pep-0570.rst?plain=1#L' in html
    assert "<mark>" in html and 'rel="noopener noreferrer"' in html
    assert re.search(r'class="rank">1\.</span>', html)
    assert no_raw_markup(html)


@pytest.mark.parametrize("method", ["hybrid", "semantic"])
def test_every_method_works_on_the_page(peps, method):
    response = peps.get("/", params={"q": "how do I make sure a file gets closed", "method": method})
    assert response.status_code == 200 and response.text.count('class="result"') == 10
    assert f'<option value="{method}" selected>' in response.text


def test_the_query_is_kept_in_the_box_escaped(peps):
    html = peps.get("/", params={"q": '"><script>x</script> walrus'}).text
    assert 'value="&quot;&gt;&lt;script&gt;x&lt;/script&gt; walrus"' in html
    assert no_raw_markup(html)


def test_document_text_can_never_become_markup(hostile):
    html = hostile.get("/", params={"q": "walrus"}).text
    assert no_raw_markup(html)
    assert "&lt;script&gt;alert(&#x27;title&#x27;)&lt;/script&gt; Walrus &amp; Friends" in html
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in html
    assert "<mark>walrus</mark>" in html
    assert "Intro &lt;i&gt;x&lt;/i&gt;" in html and "/notes/&lt;b&gt;h&lt;/b&gt;.txt" in html
    assert 'href="https://x.example/&quot;onmouseover=&quot;alert(1)"' in html and 'onmouseover="' not in html


@pytest.mark.parametrize(("params", "status", "message"), [
    ({"q": "?! :="}, 400, "Nothing to search for"),
    ({"q": "walrus", "method": "neural"}, 400, "Unknown search method"),
    ({"q": "walrus", "n": "0"}, 400, "between 1 and 100"),
    ({"q": "walrus", "n": "lots"}, 400, "between 1 and 100"),
    ({"q": "w" * (MAX_QUERY_CHARS + 1)}, 400, "too long"),
    ({"q": "zzzqqqxx"}, 200, "No passage matches"),
])
def test_bad_or_empty_searches_explain_themselves(peps, params, status, message):
    response = peps.get("/", params=params)
    assert response.status_code == status and message in response.text
    assert no_raw_markup(response.text)


def test_a_missing_library_is_explained(tmp_path):
    with TestClient(create_app(tmp_path / "none.db")) as client:
        page = client.get("/", params={"q": "walrus"})
        assert page.status_code == 503 and "evfind ingest --peps" in page.text
        assert client.get("/").status_code == 503
        assert client.get("/health").json()["status"] == "unavailable"
        assert client.get("/api/search", params={"q": "walrus"}).status_code == 503


def test_the_semantic_index_is_built_once_for_all_requests(hostile, monkeypatch):
    monkeypatch.setattr(semantic, "DIMENSIONS", 2)
    calls = []
    real = semantic.load_index
    monkeypatch.setattr(semantic, "load_index", lambda library, **kw: calls.append(1) or real(library, **kw))
    for query in ("automobile", "fruit", "engine"):
        assert hostile.get("/", params={"q": query, "method": "hybrid"}).status_code == 200
    assert calls == [1]


def test_a_library_too_small_for_meaning_says_so(tmp_path):
    path = tmp_path / "lib.db"
    with Library.open(path, create=True) as lib:
        lib.put("file:a", "text", "A", "a.txt", Extracted(passages=[Passage(0, "walrus")]))
    with TestClient(create_app(path)) as client:
        response = client.get("/", params={"q": "walrus", "method": "semantic"})
        assert response.status_code == 400 and "at least 3 passages" in response.text
        assert client.get("/", params={"q": "walrus"}).status_code == 200


def test_the_stylesheet_is_served(peps):
    response = peps.get("/style.css")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/css")
    assert "prefers-color-scheme: dark" in response.text


# --- the API -----------------------------------------------------------------------------------


def test_the_api_returns_hits_with_highlight_offsets(peps):
    body = peps.get("/api/search", params={"q": "walrus operator", "limit": 3}).json()
    assert body["method"] == "keyword" and len(body["hits"]) == 3 and body["documents_matched"] > 3
    hit = body["hits"][0]
    assert set(hit) >= {"rank", "score", "key", "title", "place", "url", "source_url", "excerpt", "highlights"}
    assert hit["highlights"]
    for start, end in hit["highlights"]:
        assert hit["excerpt"][start:end].lower() in {"walrus", "operator", "operators"}
    assert MARK_START not in hit["excerpt"] and MARK_END not in hit["excerpt"]


@pytest.mark.parametrize("params", [{"q": ""}, {"q": "w", "limit": 0}, {"q": "w", "limit": 101},
                                    {"q": "w", "method": "neural"}, {"q": "w" * (MAX_QUERY_CHARS + 1)}, {}])
def test_the_api_validates_its_parameters(peps, params):
    assert peps.get("/api/search", params=params).status_code == 422


def test_the_api_reports_a_query_without_words(peps):
    response = peps.get("/api/search", params={"q": "?!"})
    assert response.status_code == 400 and "nothing to search for" in response.json()["detail"]


def test_the_api_can_return_every_passage(peps):
    hits = peps.get("/api/search", params={"q": "walrus", "limit": 5, "all_passages": "true"}).json()["hits"]
    assert len(hits) == 5 and len({h["key"] for h in hits}) < 5


def test_health(peps):
    assert peps.get("/health").json() == {"status": "ok", "documents": 712, "passages": 17165}


# --- excerpts ----------------------------------------------------------------------------------


def test_excerpt_html_escapes_first_then_marks():
    assert excerpt_html(f"a <b> {MARK_START}c{MARK_END} &") == "a &lt;b&gt; <mark>c</mark> &amp;"


def test_excerpt_spans_on_one_line():
    assert excerpt_spans(f"one\n {MARK_START}two{MARK_END} three {MARK_START}four{MARK_END}") == (
        "one two three four", [[4, 7], [14, 18]])


plain = st.text(alphabet=st.characters(blacklist_characters=MARK_START + MARK_END, blacklist_categories=("Cs",)),
                max_size=20)


@given(st.lists(st.tuples(plain, plain), max_size=6), plain)
def test_spans_point_exactly_at_the_marked_words(pieces, tail):
    marked = "".join(f"{before}{MARK_START}{word}{MARK_END}" for before, word in pieces) + tail
    text, spans = excerpt_spans(marked)
    assert text == " ".join(marked.split()).replace(MARK_START, "").replace(MARK_END, "")
    expected = re.findall(f"{MARK_START}(.*?){MARK_END}", " ".join(marked.split()), re.S)
    assert [text[s:e] for s, e in spans] == expected


# --- `evfind serve` ----------------------------------------------------------------------------


def test_serve_listens_on_this_computer_only(pep_library, monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: seen.update(kw))
    assert main(["serve", "--port", "9123", "--library", str(pep_library)]) == 0
    assert (seen["host"], seen["port"]) == ("127.0.0.1", 9123)
    assert "http://127.0.0.1:9123/" in capsys.readouterr().out


def test_serve_needs_a_library(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: pytest.fail("started without a library"))
    assert main(["serve", "--library", str(tmp_path / "none.db")]) == 1
    assert "evfind ingest --peps" in capsys.readouterr().err


@pytest.mark.parametrize("port", ["80", "70000", "web"])
def test_serve_checks_the_port(pep_library, capsys, port, monkeypatch):
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: pytest.fail("a server started"))  # never really listen
    with pytest.raises(SystemExit) as exit_info:
        main(["serve", "--port", port, "--library", str(pep_library)])
    assert exit_info.value.code == 2 and "--port" in capsys.readouterr().err
