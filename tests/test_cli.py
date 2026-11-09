import os
import subprocess
import sys

import pytest

from evfind import __version__
from evfind.cli import main


def test_ingest_peps_then_stats(tmp_path, capsys):
    library = tmp_path / "lib.db"
    assert main(["ingest", "--peps", "--library", str(library)]) == 0
    out = capsys.readouterr().out
    assert "imported 712 PEPs as" in out and "at d94bba4, 2026-11-06" in out
    assert main(["stats", "--library", str(library)]) == 0
    assert capsys.readouterr().out.startswith("712 documents (712 indexed, 0 unusable)")


def test_ingest_files_lists_what_wasnt_imported(tmp_path, capsys):
    (tmp_path / "good.txt").write_text("Some text.", encoding="utf-8")
    (tmp_path / "bad.docx").write_bytes(b"PK")
    library = tmp_path / "lib.db"
    code = main(["ingest", str(tmp_path / "good.txt"), str(tmp_path / "bad.docx"), "--library", str(library)])
    out = capsys.readouterr().out
    assert code == 0
    assert "imported 1 of 2 files as 1 passage\n" in out
    assert f"not imported: {tmp_path / 'bad.docx'}: .docx isn't supported" in out
    assert main(["stats", "--library", str(library)]) == 0
    stats = capsys.readouterr().out
    assert stats.startswith("2 documents (1 indexed, 1 unusable), 1 passage, 2 words\n")
    assert ".docx isn't supported" in stats


def test_ingest_fails_when_nothing_could_be_imported(tmp_path, capsys):
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")
    assert main(["ingest", str(tmp_path / "empty.txt"), "--library", str(tmp_path / "lib.db")]) == 1
    assert "not imported" in capsys.readouterr().out


def test_ingest_without_anything_to_import_is_a_usage_error(tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["ingest", "--library", str(tmp_path / "lib.db")])
    assert exit_info.value.code == 2
    assert "give files to import, or --peps" in capsys.readouterr().err
    assert not (tmp_path / "lib.db").exists()


def test_stats_on_a_missing_library_explains(tmp_path, capsys):
    assert main(["stats", "--library", str(tmp_path / "nope.db")]) == 1
    assert "evfind ingest --peps" in capsys.readouterr().err


def test_ingest_into_a_file_that_isnt_a_library_fails_cleanly(tmp_path, capsys):
    (tmp_path / "x.db").write_bytes(b"not sqlite" * 100)
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    assert main(["ingest", str(tmp_path / "a.txt"), "--library", str(tmp_path / "x.db")]) == 1
    assert "isn't a usable library" in capsys.readouterr().err


def test_build_snapshot_command(tmp_path, capsys):
    from test_peps import make_checkout, pep_text

    checkout = make_checkout(tmp_path / "repo", {"pep-9001.rst": pep_text()})
    assert main(["build-snapshot", str(checkout), "-o", str(tmp_path / "s.jsonl.gz")]) == 0
    assert "1 of 1 PEPs included" in capsys.readouterr().out
    assert main(["build-snapshot", str(tmp_path / "nowhere"), "-o", str(tmp_path / "t.jsonl.gz")]) == 1
    assert "no PEP files" in capsys.readouterr().err


@pytest.mark.parametrize("buffering", [[], ["-u"]], ids=["buffered", "unbuffered"])
def test_a_reader_that_goes_away_early_gets_no_traceback(pep_library, buffering):
    """`evfind stats | head -1`: the output pipe closes before evfind has finished writing."""
    library = pep_library
    # without PYTHONUNBUFFERED, so "buffered" really is buffered wherever the tests run
    env = {name: value for name, value in os.environ.items() if name != "PYTHONUNBUFFERED"}
    proc = subprocess.Popen([sys.executable, *buffering, "-m", "evfind.cli", "stats", "--library", str(library)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    proc.stdout.close()
    stderr = proc.stderr.read()
    proc.stderr.close()
    assert proc.wait(timeout=60) == 1
    assert b"Traceback" not in stderr and b"BrokenPipeError" not in stderr, stderr.decode()


def test_search_shows_where_each_passage_came_from(pep_library, capsys):
    assert main(["search", "assignment", "expressions", "--limit", "3", "--library", str(pep_library)]) == 0
    out = capsys.readouterr().out
    assert "match. The best passage from each of the top 3 documents:" in out.splitlines()[0]
    assert "PEP 572: Assignment Expressions" in out
    assert "https://peps.python.org/pep-0572/" in out
    assert "source: https://github.com/python/peps/blob/d94bba4a5dff241fd418f30223d3bf05de27a771/peps/pep-0572.rst" in out
    assert "**assignment**" in out.lower() and "more matching passages in this document" in out
    assert [line[:4] for line in out.splitlines() if line[:4].strip().rstrip(".").isdigit()] == ["  1.", "  2.", "  3."]


def test_search_all_passages_can_repeat_a_document(pep_library, capsys):
    assert main(["search", "walrus", "--all-passages", "-n", "5", "--library", str(pep_library)]) == 0
    out = capsys.readouterr().out
    assert "The top 5 passages:" in out and "in this document" not in out
    titles = [line.split(". ", 1)[1] for line in out.splitlines() if line[:4].strip().rstrip(".").isdigit()]
    assert len(titles) == 5 and len(set(titles)) < 5  # PEP 622 has several of the best walrus passages


def test_the_summary_line_reads_well_for_one_result(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("walrus", encoding="utf-8")
    library = str(tmp_path / "lib.db")
    assert main(["ingest", str(tmp_path / "a.txt"), "--library", library]) == 0
    capsys.readouterr()
    assert main(["search", "walrus", "--library", library]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "1 passage in 1 document matches. The best passage from the top document:"
    assert main(["search", "walrus", "--all-passages", "--library", library]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "1 passage in 1 document matches. The top passage:"


def test_search_with_no_matches_says_so(pep_library, capsys):
    assert main(["search", "zzzqqqxx", "--library", str(pep_library)]) == 0
    assert capsys.readouterr().out == "No passage matches 'zzzqqqxx'.\n"


def test_search_for_nothing_is_a_usage_error(pep_library, capsys):
    assert main(["search", ":=", "?", "--library", str(pep_library)]) == 2
    assert "nothing to search for" in capsys.readouterr().err


@pytest.mark.parametrize("limit", ["0", "101", "ten"])
def test_search_checks_the_limit(pep_library, capsys, limit):
    with pytest.raises(SystemExit) as exit_info:
        main(["search", "walrus", "--limit", limit, "--library", str(pep_library)])
    assert exit_info.value.code == 2
    assert "--limit" in capsys.readouterr().err


def test_search_needs_a_library(tmp_path, capsys):
    assert main(["search", "walrus", "--library", str(tmp_path / "none.db")]) == 1
    assert "evfind ingest --peps" in capsys.readouterr().err


def test_search_uses_bold_only_in_a_terminal(monkeypatch):
    from evfind.cli import _emphasis

    class Terminal:
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdout", Terminal())
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert _emphasis() == ("\x1b[1m", "\x1b[0m")
    monkeypatch.setenv("NO_COLOR", "1")
    assert _emphasis() == ("**", "**")


def test_eval_reports_a_split_with_the_baseline_and_misses(pep_library, capsys):
    """On the tuning split, to keep it quick; test_evaluate.py checks the test split's numbers."""
    assert main(["eval", "--split", "tuning", "--compare", "--misses", "--library", str(pep_library)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("49 tuning questions (30 using the PEP's terms, 19 paraphrased); "
                          "keyword search, weights title 8, section 1, text 1")
    assert "mrr        0.797 [" in out
    assert "Change from equal weights (title 1, section 1, text 1), paired:\n  recall@5" in out
    assert "  mrr        +0.076 [" in out
    assert "\n4 questions with no relevant document in the top 10:\n" in out and "wanted pep-" in out


def test_tune_uses_only_the_tuning_questions(pep_library, capsys, monkeypatch):
    monkeypatch.setattr("evfind.evaluate.WEIGHT_GRID", ((1.0, 1.0, 1.0), (8.0, 1.0, 1.0)))  # the full grid takes a minute
    assert main(["tune", "--library", str(pep_library)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "Mean reciprocal rank on 49 tuning questions, best first:"
    assert [line.split() for line in lines[1:]] == [["title", "8,", "section", "1,", "text", "1", "0.797"],
                                                    ["title", "1,", "section", "1,", "text", "1", "0.721"]]


def test_eval_needs_the_peps(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    library = str(tmp_path / "lib.db")
    main(["ingest", str(tmp_path / "a.txt"), "--library", library])
    capsys.readouterr()
    assert main(["eval", "--library", library]) == 1
    assert "has no PEPs" in capsys.readouterr().err


@pytest.mark.parametrize("weights", ["8,1", "8,0,1", "a,b,c"])
def test_eval_checks_the_weights(pep_library, capsys, weights):
    with pytest.raises(SystemExit) as exit_info:
        main(["eval", "--weights", weights, "--library", str(pep_library)])
    assert exit_info.value.code == 2 and "--weights" in capsys.readouterr().err


def test_search_by_meaning_says_so(pep_library, capsys):
    assert main(["search", "walrus", "operator", "--method", "semantic", "-n", "2", "--library", str(pep_library)]) == 0
    out = capsys.readouterr()
    assert "are related by meaning (LSA). The best passage from each of the top 2 documents:" in out.out
    assert out.err == ""  # the index was already built: nothing to report


def test_hybrid_search_command(pep_library, capsys):
    assert main(["search", "walrus", "operator", "--method", "hybrid", "-n", "3", "--library", str(pep_library)]) == 0
    assert "match. The best passage from each of the top 3 documents:" in capsys.readouterr().out


def test_eval_hybrid_compares_with_keyword_search(pep_library, capsys):
    assert main(["eval", "--split", "tuning", "--method", "hybrid", "--compare", "--library", str(pep_library)]) == 0
    out = capsys.readouterr().out
    assert "hybrid search, 100 LSA dimensions, keyword weight 2, c 10" in out.splitlines()[0]
    assert "Change from keyword search, paired:" in out
    changes = out.split("Change from keyword search, paired:")[1].split()
    assert any(value not in ("+0.000", "[+0.000,", "+0.000]") for value in changes[1::4])  # really compared


def test_eval_dimensions_need_a_semantic_method(pep_library, capsys):
    assert main(["eval", "--dimensions", "50", "--library", str(pep_library)]) == 1
    assert "--dimensions needs --method semantic or hybrid" in capsys.readouterr().err


def test_tune_hybrid_lists_lsa_and_fusion_settings(pep_library, capsys, monkeypatch):
    monkeypatch.setattr("evfind.evaluate.DIMENSION_GRID", (100,))      # the full grid takes minutes
    monkeypatch.setattr("evfind.evaluate.FUSION_GRID", ((2.0, 10.0),))
    assert main(["tune", "--method", "hybrid", "--library", str(pep_library)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "Mean reciprocal rank on 49 tuning questions, best first:"
    assert [line.rsplit(None, 1)[0].strip() for line in lines[1:]] == [
        "hybrid, 100 dimensions, keyword weight 2, c 10", "semantic, 100 dimensions"]
    assert lines[1].split()[-1] == "0.788"


def test_the_installed_command_runs():
    result = subprocess.run([sys.executable, "-m", "evfind.cli", "--version"], capture_output=True, text=True,
                            timeout=60)
    assert (result.returncode, result.stdout.strip()) == (0, f"evfind {__version__}")
