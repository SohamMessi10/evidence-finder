"""Measuring retrieval quality on labelled questions.

Each question names the PEPs that answer it. The questions were written, labelled and split
into a tuning set and a test set before any search was run on them. Settings are chosen with
the tuning set only; the test set is for reporting.

Ranking is by document: each document is ranked by its best passage (the default search). The
metrics, per question, then averaged:

    Recall@k   the share of the question's relevant documents in the top k
    MRR        1 / the rank of the first relevant document (0 if none in the top 100)
    nDCG@10    discounted gain of relevant documents in the top 10, against the ideal order

Uncertainty is a 95% percentile bootstrap over questions, with a fixed seed so the interval is
reproducible. A comparison of two settings resamples the per-question differences (paired).
"""

from __future__ import annotations

import itertools
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path

from evfind.search import MAX_LIMIT, search
from evfind.store import Library

DEFAULT_QUESTIONS = Path(__file__).resolve().parent / "data" / "questions.jsonl"
SPLITS = ("tuning", "test")
KINDS = ("terms", "paraphrase")
METRICS = ("recall@5", "recall@10", "mrr", "ndcg@10")
BOOTSTRAP_SAMPLES = 2000
BOOTSTRAP_SEED = 20261109
# Column weights tried when tuning (title, section, text). Text stays 1: only the ratios matter.
WEIGHT_GRID = tuple((t, s, 1.0) for t, s in itertools.product((1.0, 2.0, 4.0, 8.0, 16.0, 32.0), (1.0, 2.0, 4.0)))


class EvaluationError(ValueError):
    """The questions file can't be used."""


@dataclass(frozen=True)
class Question:
    id: str
    split: str
    kind: str
    text: str
    relevant: tuple[str, ...]


def load_questions(path: Path = DEFAULT_QUESTIONS) -> list[Question]:
    questions, seen = [], set()
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise EvaluationError(f"{path} can't be read ({exc.strerror})") from None
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            question = Question(row["id"], row["split"], row["kind"], row["question"], tuple(row["relevant"]))
        except (ValueError, KeyError, TypeError) as exc:
            raise EvaluationError(f"{path}, line {number}: not a question ({exc})") from None
        if question.id in seen:
            raise EvaluationError(f"{path}, line {number}: the id {question.id} is used twice")
        if question.split not in SPLITS or question.kind not in KINDS:
            raise EvaluationError(f"{path}, line {number}: split must be one of {SPLITS} and kind one of {KINDS}")
        if not question.relevant or not question.text.strip():
            raise EvaluationError(f"{path}, line {number}: a question needs text and at least one relevant document")
        seen.add(question.id)
        questions.append(question)
    return questions


def recall_at(ranked: list[str], relevant: tuple[str, ...], k: int) -> float:
    return len(set(ranked[:k]) & set(relevant)) / len(set(relevant))


def reciprocal_rank(ranked: list[str], relevant: tuple[str, ...]) -> float:
    return next((1 / rank for rank, key in enumerate(ranked, start=1) if key in relevant), 0.0)


def ndcg_at(ranked: list[str], relevant: tuple[str, ...], k: int = 10) -> float:
    gain = sum(1 / math.log2(rank + 1) for rank, key in enumerate(ranked[:k], start=1) if key in relevant)
    ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(len(set(relevant)), k) + 1))
    return gain / ideal


def score(ranked: list[str], relevant: tuple[str, ...]) -> dict[str, float]:
    return {"recall@5": recall_at(ranked, relevant, 5), "recall@10": recall_at(ranked, relevant, 10),
            "mrr": reciprocal_rank(ranked, relevant), "ndcg@10": ndcg_at(ranked, relevant, 10)}


def bootstrap_interval(values: list[float], *, samples: int = BOOTSTRAP_SAMPLES,
                       seed: int = BOOTSTRAP_SEED) -> tuple[float, float]:
    """A 95% percentile bootstrap interval for the mean."""
    if not values:
        raise ValueError("no values")
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(samples))
    return means[int(0.025 * samples)], means[int(0.975 * samples) - 1]


@dataclass(frozen=True)
class Outcome:
    question: Question
    ranked: list[str]          # document keys, best first, up to 100
    scores: dict[str, float]


def run(library: Library, questions: list[Question], weights: tuple[float, float, float]) -> list[Outcome]:
    outcomes = []
    for question in questions:
        hits = search(library, question.text, limit=MAX_LIMIT, per_document=1, weights=weights).hits
        ranked = [hit.key for hit in hits]
        outcomes.append(Outcome(question, ranked, score(ranked, question.relevant)))
    return outcomes


def summarise(outcomes: list[Outcome]) -> dict[str, tuple[float, float, float]]:
    """Each metric's mean and its 95% bootstrap interval."""
    summary = {}
    for metric in METRICS:
        values = [o.scores[metric] for o in outcomes]
        summary[metric] = (sum(values) / len(values), *bootstrap_interval(values))
    return summary


def compare(before: list[Outcome], after: list[Outcome], metric: str) -> tuple[float, float, float]:
    """The mean per-question change in a metric, and its paired 95% bootstrap interval."""
    if [o.question.id for o in before] != [o.question.id for o in after]:
        raise ValueError("the two runs must be on the same questions, in the same order")
    differences = [b.scores[metric] - a.scores[metric] for a, b in zip(before, after)]
    return (sum(differences) / len(differences), *bootstrap_interval(differences))


def tune(library: Library, questions: list[Question], grid=None, metric: str = "mrr"):
    """Every weight setting's mean `metric` on these questions, best first.

    Ties go to the setting listed first in the grid, which lists smaller weights first.
    Refuses test questions: settings may only be chosen on the tuning set.
    """
    if any(q.split != "tuning" for q in questions):
        raise EvaluationError("weights may only be tuned on tuning questions")
    grid = WEIGHT_GRID if grid is None else grid
    results = []
    for order, weights in enumerate(grid):
        outcomes = run(library, questions, weights)
        results.append((sum(o.scores[metric] for o in outcomes) / len(outcomes), -order, weights))
    results.sort(reverse=True)
    return [(weights, mean) for mean, _, weights in results]
