import json
import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from evfind.evaluate import (
    DEFAULT_QUESTIONS,
    EvaluationError,
    Question,
    bootstrap_interval,
    compare,
    load_questions,
    ndcg_at,
    recall_at,
    reciprocal_rank,
    run,
    score,
    summarise,
    tune,
)
from evfind.extract import Extracted, Passage
from evfind.ingest import DEFAULT_SNAPSHOT
from evfind.peps import read_snapshot
from evfind.search import DEFAULT_WEIGHTS, EQUAL_WEIGHTS
from evfind.store import Library

# --- metrics, against values worked out by hand ----------------------------------------------


def test_recall_counts_relevant_documents_in_the_top_k():
    assert recall_at(["a", "x", "b", "y"], ("a", "b"), 2) == 0.5
    assert recall_at(["a", "x", "b", "y"], ("a", "b"), 3) == 1.0
    assert recall_at(["x"], ("a",), 10) == 0.0


def test_reciprocal_rank_is_one_over_the_first_relevant_rank():
    assert reciprocal_rank(["x", "y", "a", "b"], ("a", "b")) == 1 / 3
    assert reciprocal_rank(["a"], ("a",)) == 1.0
    assert reciprocal_rank(["x", "y"], ("a",)) == 0.0


def test_ndcg_discounts_by_rank_against_the_ideal():
    # relevant at ranks 1 and 3: (1 + 1/log2 4) / (1 + 1/log2 3)
    assert ndcg_at(["a", "x", "b"], ("a", "b")) == pytest.approx((1 + 1 / 2) / (1 + 1 / math.log2(3)))
    assert ndcg_at(["a", "b"], ("a", "b")) == 1.0
    assert ndcg_at(["x"] * 10 + ["a"], ("a",)) == 0.0  # rank 11 is past the cut-off


def test_ndcg_caps_the_ideal_at_the_cut_off():
    """With 12 relevant documents, a top 10 of relevant ones is already perfect."""
    relevant = tuple(f"d{i}" for i in range(12))
    assert ndcg_at(list(relevant), relevant) == 1.0


def test_the_bootstrap_interval_is_the_middle_95_percent():
    # 50 zeros and 50 ones: mean 0.5, standard error 0.05, so about 0.5 ± 0.098
    assert bootstrap_interval([0.0] * 50 + [1.0] * 50) == (0.4, 0.6)


def test_the_bootstrap_seed_fixes_the_interval():
    values = [i / 100 for i in range(100)]
    assert bootstrap_interval(values) == bootstrap_interval(values)
    assert bootstrap_interval(values) != bootstrap_interval(values, seed=1)


def test_repeated_labels_count_once():
    assert recall_at(["a"], ("a", "a"), 5) == 1.0 and ndcg_at(["a"], ("a", "a")) == 1.0


keys = st.sampled_from(list("abcdefghijklmnop"))


@given(st.lists(keys, unique=True, max_size=16), st.lists(keys, min_size=1, max_size=4).map(tuple))
def test_every_metric_is_between_0_and_1_and_consistent(ranked, relevant):
    s = score(ranked, relevant)
    assert all(0.0 <= value <= 1.0 for value in s.values())
    assert s["recall@5"] <= s["recall@10"]
    assert (s["mrr"] == 1.0) == bool(ranked and ranked[0] in relevant)
    assert (s["recall@10"] == 0.0) == (s["ndcg@10"] == 0.0)


def test_the_bootstrap_is_reproducible_and_brackets_the_mean():
    values = [0.0, 1.0, 1.0, 0.5, 0.25, 1.0, 0.0, 1.0]
    low, high = bootstrap_interval(values)
    assert bootstrap_interval(values) == (low, high)
    assert low < sum(values) / len(values) < high
    assert bootstrap_interval([0.7] * 5) == (0.7, 0.7)
    with pytest.raises(ValueError):
        bootstrap_interval([])


# --- the questions --------------------------------------------------------------------------


def test_the_bundled_questions_are_complete_and_split_before_use():
    questions = load_questions()
    assert len(questions) == 145 and len({q.id for q in questions}) == 145
    count = lambda split, kind: sum(q.split == split and q.kind == kind for q in questions)  # noqa: E731
    assert (count("tuning", "terms"), count("tuning", "paraphrase")) == (30, 19)
    assert (count("test", "terms"), count("test", "paraphrase")) == (59, 37)


def test_every_label_names_a_pep_in_the_corpus():
    keys = {pep.key for pep in read_snapshot(DEFAULT_SNAPSHOT)[1]}
    for question in load_questions():
        assert set(question.relevant) <= keys, question.id


def write(tmp_path, rows):
    path = tmp_path / "q.jsonl"
    path.write_text("".join((row if isinstance(row, str) else json.dumps(row)) + "\n" for row in rows),
                    encoding="utf-8")
    return path


GOOD = {"id": "q1", "split": "tuning", "kind": "terms", "question": "Which?", "relevant": ["pep-0008"]}


@pytest.mark.parametrize(("rows", "message"), [
    ([GOOD, GOOD], "used twice"),
    ([GOOD | {"split": "train"}], "split must be one of"),
    ([GOOD | {"kind": "other"}], "kind one of"),
    ([GOOD | {"relevant": []}], "at least one relevant"),
    ([GOOD | {"question": "  "}], "needs text"),
    (["{not json"], "not a question"),
    ([{"id": "q1"}], "not a question"),
])
def test_a_bad_questions_file_is_refused(tmp_path, rows, message):
    with pytest.raises(EvaluationError, match=message):
        load_questions(write(tmp_path, rows))


def test_a_missing_questions_file_is_refused(tmp_path):
    with pytest.raises(EvaluationError, match="can't be read"):
        load_questions(tmp_path / "none.jsonl")


# --- running ----------------------------------------------------------------------------------


@pytest.fixture
def small_library():
    with Library.open(":memory:", create=True) as lib:
        for key, text in [("pep-0001", "the walrus operator"), ("pep-0002", "walrus walrus facts"),
                          ("pep-0003", "nothing here at all")]:
            lib.put(key, "pep", key, key, Extracted(passages=[Passage(0, text)]))
        yield lib


def question(id_, text, relevant, split="tuning"):
    return Question(id_, split, "terms", text, tuple(relevant))


def test_run_scores_each_question(small_library):
    outcomes = run(small_library, [question("a", "operator", ["pep-0001"]),
                                   question("b", "facts", ["pep-0003"])], EQUAL_WEIGHTS)
    assert [o.ranked for o in outcomes] == [["pep-0001"], ["pep-0002"]]
    assert [o.scores["mrr"] for o in outcomes] == [1.0, 0.0]
    summary = summarise(outcomes)
    assert summary["mrr"][0] == 0.5 and summary["mrr"][1] <= 0.5 <= summary["mrr"][2]


def test_compare_pairs_the_questions(small_library):
    questions = [question("a", "operator", ["pep-0001"]), question("b", "facts", ["pep-0003"])]
    before, after = run(small_library, questions, EQUAL_WEIGHTS), run(small_library, questions, (8.0, 1.0, 1.0))
    assert compare(before, after, "mrr") == (0.0, 0.0, 0.0)
    with pytest.raises(ValueError, match="same questions"):
        compare(before, after[::-1], "mrr")


def test_tuning_refuses_test_questions(small_library):
    with pytest.raises(EvaluationError, match="only be tuned on tuning questions"):
        tune(small_library, [question("a", "walrus", ["pep-0001"], split="test")])


def test_tuning_returns_every_setting_best_first(small_library):
    grid = [(1.0, 1.0, 1.0), (2.0, 1.0, 1.0)]
    ranking = tune(small_library, [question("a", "walrus", ["pep-0002"])], grid=grid)
    assert [w for w, _ in ranking] == grid  # a tie keeps the grid's order
    assert all(mrr == 1.0 for _, mrr in ranking)


# --- the published numbers ------------------------------------------------------------------


def test_the_published_test_results_are_what_the_code_produces(pep_library):
    """docs/evaluation.md reports these; if ranking changes, this fails until the docs do too."""
    questions = [q for q in load_questions(DEFAULT_QUESTIONS) if q.split == "test"]
    with Library.open(pep_library) as library:
        tuned = run(library, questions, DEFAULT_WEIGHTS)
        equal = run(library, questions, EQUAL_WEIGHTS)
    means = {metric: round(value[0], 3) for metric, value in summarise(tuned).items()}
    assert means == {"recall@5": 0.800, "recall@10": 0.852, "mrr": 0.708, "ndcg@10": 0.733}
    assert round(compare(equal, tuned, "mrr")[0], 3) == 0.030
    assert sum(o.scores["recall@10"] == 0 for o in tuned) == 13
