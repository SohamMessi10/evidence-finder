# Evaluation

How good is the search, measured on labelled questions, with the uncertainty stated. Every
number here is produced by the commands at the end, and a test fails if the code stops
producing them.

## The questions

`src/evfind/data/questions.jsonl` holds 145 questions about the PEPs. Each names the PEP or
PEPs that answer it.

- **Who wrote them:** Claude, the AI assistant building this project, from knowledge of the
  PEPs. They are not independent human relevance judgments, and they come from a single
  author, which a published benchmark would not accept. They are good enough to compare
  settings of this search on the same questions, which is what they are used for here.
- **Two kinds.** 89 use the PEP's own terms ("Which PEP added the with statement?"). 56 are
  paraphrased, avoiding the title's words ("How do I make sure a file gets closed even when an
  error happens in the block using it?").
- **Labels name the primary answer** (1 PEP for most; up to 3 when several genuinely answer,
  like the three pattern-matching PEPs). Related PEPs aren't labelled, so the scores are
  conservative: a reasonable related answer counts as a miss.
- **Checked before any search was run on them.** Every label was checked by its PEP's title and
  its presence in the corpus. One question was dropped because its answer, PEP 202 (list
  comprehensions), isn't in the corpus: it has no license statement. Re-reading removed two
  labels that didn't answer their questions: PEP 409 (it suppresses an exception's context
  rather than keeping it) and PEP 525 (async generators, for "how do I make an object
  awaitable").
- **Split once, before use**, by a seeded shuffle (seed 20261109) within each kind: 49
  **tuning** questions (30 terms, 19 paraphrased) and 96 **test** questions (59 terms, 37
  paraphrased). Settings are chosen on tuning questions only; `evfind tune` refuses test
  questions.

## Metrics

Documents are ranked by their best passage, the default search, up to 100 deep. Per question:

| Metric | Meaning |
|---|---|
| Recall@5, Recall@10 | the share of the question's labelled PEPs in the top 5 or 10 |
| MRR | 1 / the rank of the first labelled PEP (0 if not in the top 100) |
| nDCG@10 | labelled PEPs in the top 10, discounted by rank, against the best possible order |

Each is averaged over questions. The 95% intervals are percentile bootstraps over questions
(2,000 resamples, fixed seed). A comparison of two settings resamples the per-question
differences, so it is paired.

## Tuning: the column weights

BM25 in FTS5 weights each column (title, section, text); its own parameters are fixed at
k1 = 1.2 and b = 0.75. `evfind tune` tried 18 settings on the 49 tuning questions:

| Title | Section | Text | MRR (tuning) |
|---:|---:|---:|---:|
| 8 | 1 | 1 | **0.797** |
| 8 | 2 | 1 | 0.795 |
| 32 | 1 | 1 | 0.789 |
| 16 | 1 | 1 | 0.788 |
| … | | | |
| 1 | 1 | 1 (milestone 2's equal weights) | 0.721 |
| 1 | 4 | 1 | 0.692 (the worst) |

On the tuning questions, title 8 beat equal weights by +0.076 MRR (paired 95% interval
[+0.025, +0.135]). The first grid stopped at 8; it was extended to 32 before choosing, because
a best value at the edge of a grid says the grid was too small. Title 8, section 1, text 1
became the default.

**A side effect, found and fixed before testing.** FTS5's `bm25()` normalises by the length of
the whole row, not of each column. With a heavy title weight, every passage of a document
matches the title equally, so the document's best-scoring passage was simply its shortest: for
"assignment expressions", PEP 577 was represented by its one-line copyright notice. Ranking
*documents* and choosing their *passage* are now separate. Documents are ranked by their best
passage under the tuned weights (what the metrics measure). The passage shown is the one whose
section and text match best, ignoring the title. The metrics can't measure that choice, because
the labels are per document, so it is a design decision rather than a measured one.

## Test results

Run once, on the 96 test questions, after the weights were fixed:

| | All 96 | Terms (59) | Paraphrased (37) |
|---|---|---|---|
| Recall@5 | 0.800 [0.719, 0.875] | 0.966 [0.915, 1.000] | 0.536 [0.387, 0.694] |
| Recall@10 | 0.852 [0.781, 0.917] | 1.000 [1.000, 1.000] | 0.617 [0.468, 0.766] |
| MRR | 0.708 [0.631, 0.782] | 0.894 [0.824, 0.952] | 0.411 [0.292, 0.548] |
| nDCG@10 | 0.733 [0.660, 0.803] | 0.919 [0.866, 0.962] | 0.438 [0.317, 0.574] |

The change from equal weights on the test questions, paired:

| Metric | Change | 95% interval |
|---|---:|---|
| Recall@5 | +0.000 | [+0.000, +0.000] |
| Recall@10 | −0.021 | [−0.052, +0.000] |
| MRR | +0.030 | [−0.008, +0.072] |
| nDCG@10 | +0.017 | [−0.013, +0.049] |

### What this shows

1. **Keyword search is excellent when the question uses the document's words, and weak when it
   doesn't.** Questions using the PEP's terms find their PEP in the top 10 every time, usually
   first (MRR 0.894). For paraphrased questions, only 62% of the labelled PEPs are in the
   top 10 (MRR 0.411). All 13 test questions with no labelled PEP in the top 10 are paraphrased: the gap
   that semantic search, in milestone 4, has to close.
2. **The tuned weights did not clearly carry over to the test set.** The tuning gain of +0.076
   MRR shrank to +0.030, and its interval includes zero; Recall@10 dipped slightly. Tuning on
   49 questions overfitted them somewhat. The tuned weights stay the default, since what evidence
   there is favours them, weakly. But this evaluation doesn't show they are better, and nothing
   here claims it.
3. **Some misses are fair answers marked wrong.** "Where does Python put compiled bytecode
   files?" returned PEP 304 and PEP 488, the latter about removing `.pyo` files; the label lists
   only PEP 3147. Labels weren't added after seeing these results: changing the test set's
   labels in response to its results would make it a second tuning set.

## Limits of this evaluation

- One author wrote and labelled every question, with knowledge of the PEPs; real users' wording
  may be harder, or easier.
- 96 test questions give wide intervals: differences under about 0.05 MRR can't be told apart.
- The test set has now been used once. Milestone 4's comparison of keyword, semantic and hybrid
  search will use it once more, with all three methods' settings chosen on the tuning
  questions first.

## Reproducing

```
evfind ingest --peps
evfind tune
evfind eval --split tuning --compare
evfind eval --split test --compare --misses
```
