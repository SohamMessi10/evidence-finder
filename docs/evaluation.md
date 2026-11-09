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

## Milestone 4: semantic and hybrid search

**Semantic search** here is LSA: TF-IDF over each passage's title, section and text (sublinear
term frequency, English stop words removed, words in at least two passages), compressed by a
truncated SVD and compared by cosine similarity. It is a classical method, chosen because the
build machine can't download neural embedding models, so it is called LSA, not "embeddings".
**Hybrid search** fuses the keyword and LSA document rankings by weighted reciprocal rank
fusion: a document scores *w* / (*c* + keyword rank) + 1 / (*c* + LSA rank).

### Tuning (the 49 tuning questions only)

`evfind tune --method hybrid` tried 7 sizes of LSA (100 to 800 dimensions) and, for each, LSA
alone and six fusion settings (*w* of 1 or 2, *c* of 10, 30 or 60):

| Setting | MRR (tuning) |
|---|---:|
| Hybrid, 100 dimensions, *w* = 2, *c* = 10 | **0.788** |
| Hybrid, 400 or 500 dimensions, *w* = 2, *c* = 10 | 0.787 |
| Keyword search alone (its weights tuned on these same questions) | 0.797 |
| LSA alone, best size (800 dimensions) | 0.680 |
| LSA alone, 100 dimensions | 0.585 |

Hybrid at 100 dimensions was chosen: by the rule (best score, ties to fewer dimensions), and in
practice a tie with 400 and 500, at a quarter of the index's size. LSA alone was evaluated at its
own best size, 800 dimensions, so it wasn't judged on a size chosen for something else.

**Keyword search stayed the default**, decided before the test run: on the tuning questions
nothing beat it. That comparison flatters keyword search, whose weights were tuned on the same
questions, which is why the test set decides.

A prototype run on the tuning questions had preferred 400 dimensions, but it fused only the top
100 of each ranking, while the search fuses the full rankings. The shipped tuning, which does
exactly what the search does, decided.

### Test results (run once, settings fixed)

| | Keyword | LSA alone (800) | Hybrid (100) |
|---|---|---|---|
| Recall@5 | 0.800 | 0.786 | 0.832 [0.757, 0.901] |
| Recall@10 | 0.852 | 0.885 | **0.915** [0.858, 0.964] |
| MRR | 0.708 | 0.675 | 0.734 [0.660, 0.804] |
| nDCG@10 | 0.733 | 0.723 | 0.769 [0.703, 0.832] |
| Questions with nothing relevant in the top 10 | 13 | | **7** |

Paired against keyword search on the same questions:

| | Hybrid − keyword | LSA alone − keyword |
|---|---|---|
| Recall@10 | **+0.062 [+0.010, +0.115]** | +0.033 [−0.028, +0.094] |
| MRR | +0.026 [−0.014, +0.068] | −0.033 [−0.092, +0.029] |
| nDCG@10 | +0.035 [−0.001, +0.072] | −0.010 [−0.063, +0.041] |

By kind, for the hybrid: questions using the PEP's terms keep Recall@10 at 1.000 (MRR 0.891,
against 0.894 for keyword search); paraphrased questions rise from Recall@10 0.617 to 0.779 and
MRR 0.411 to 0.483.

### What this shows

1. **Hybrid search finds more of the right documents.** Its Recall@10 gain over keyword search
   is the only test-set comparison whose interval lies clearly above zero, and the misses fall
   from 13 to 7, all of the gain on paraphrased questions. It doesn't clearly rank the right
   document higher at the very top: MRR's interval includes zero.
2. **LSA alone is no better than keyword search.** On paraphrased questions its MRR is lower
   (0.320 against 0.411), though it reaches more of them by rank 10. Its value is as a second
   opinion fused with keyword search, not a replacement.
3. **The default is still keyword search**, because that was fixed before the test. On this
   evidence, making hybrid the default is the natural next decision; but deciding it from these
   test results would make them a tuning set, so it is recorded as a recommendation, not done.
4. **Why only partly?** The remaining misses ("how do I make sure a file gets closed even when an
   error happens…" for the `with` statement) need knowledge of what a construct is *for*, which
   word co-occurrence across 712 PEPs doesn't capture. Neural sentence embeddings are the
   obvious next step, measured the same way, where a model can be downloaded.

## Limits of this evaluation

- One author wrote and labelled every question, with knowledge of the PEPs; real users' wording
  may be harder, or easier.
- 96 test questions give wide intervals: differences under about 0.05 MRR can't be told apart.
- The test set has been used twice: once for keyword search (milestone 3), and once for the
  three-way comparison (milestone 4), each time with every setting fixed beforehand on the tuning
  questions. Two looks are fewer than most projects take, but a fresh set of questions would be
  needed to confirm a change of default.
- LSA and hybrid numbers can differ by a few thousandths between machines (the SVD's
  floating-point sums), which the tests allow for; keyword numbers are exact.

## Reproducing

```
evfind ingest --peps
evfind tune
evfind eval --split tuning --compare
evfind eval --split test --compare --misses
evfind tune --method hybrid
evfind eval --split test --method hybrid --compare --misses
evfind eval --split test --method semantic --dimensions 800 --compare
```
