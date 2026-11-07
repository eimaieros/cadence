"""Evaluation for job-description retrieval.

Run it:

    python -m evals.retrieval

For every query in :mod:`evals.retrieval_cases` it chunks the description,
embeds the passages and the query, ranks the passages, and records where the
expected passage landed. Two numbers summarise that:

* **recall@k** -- the share of queries whose expected passage is in the top k.
  The interviewer reads the top three, so recall@3 is the one that matters.
* **MRR** -- mean reciprocal rank: 1 for first place, 1/2 for second, 1/3 for
  third. It rewards getting the right passage *first*, not just somewhere.

The floors below are a ratchet: they sit at or just under what the offline
embedder measured when they were last raised, a change that drops below them
fails, and they only ever move up.
Hard queries are reported but not held to the floor -- they exist to show the
offline embedder's limit, not to pretend it has none.

Exit code is 1 when a floor is missed, so this drops into CI unchanged.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from app.retrieval.chunking import chunk_job_description
from app.retrieval.embedding import Embedder, HashingEmbedder, rank
from evals.retrieval_cases import CASES, RetrievalCase

TOP_K = 3
# HashingEmbedder over the standard (non-hard) queries measured recall@3 1.00
# and MRR 0.97 when these were set.
RECALL_AT_3_FLOOR = 1.0
MRR_FLOOR = 0.95


@dataclass(frozen=True)
class Result:
    case: str
    query: str
    hard: bool
    position: int | None  # 1-based; None when not in the ranked list


def evaluate(cases: list[RetrievalCase], embedder: Embedder) -> list[Result]:
    results: list[Result] = []
    for case in cases:
        passages = chunk_job_description(case.description)
        vectors = embedder.embed([p.text for p in passages])
        for query in case.queries:
            expected = [i for i, p in enumerate(passages) if query.expected in p.text]
            if not expected:
                raise ValueError(f"{case.name}: no passage contains {query.expected!r}")
            ranked = rank(embedder.embed([query.text])[0], vectors, k=len(passages))
            position = next(
                (n for n, (index, _) in enumerate(ranked, start=1) if index in expected), None
            )
            results.append(Result(case.name, query.text, query.hard, position))
    return results


def recall_at(results: list[Result], k: int) -> float:
    if not results:
        return 0.0
    return sum(1 for r in results if r.position is not None and r.position <= k) / len(results)


def mean_reciprocal_rank(results: list[Result]) -> float:
    if not results:
        return 0.0
    return sum(1 / r.position for r in results if r.position) / len(results)


def main() -> int:
    results = evaluate(CASES, HashingEmbedder())
    for r in results:
        mark = " (hard)" if r.hard else ""
        print(f"  #{r.position}  {r.case:16} {r.query}{mark}")

    standard = [r for r in results if not r.hard]
    hard = [r for r in results if r.hard]
    print()
    print(f"standard queries: {len(standard)}  recall@1 {recall_at(standard, 1):.2f}  "
          f"recall@{TOP_K} {recall_at(standard, TOP_K):.2f}  MRR {mean_reciprocal_rank(standard):.2f}")
    print(f"hard queries:     {len(hard)}  recall@1 {recall_at(hard, 1):.2f}  "
          f"recall@{TOP_K} {recall_at(hard, TOP_K):.2f}  MRR {mean_reciprocal_rank(hard):.2f}")

    failed = []
    if recall_at(standard, TOP_K) < RECALL_AT_3_FLOOR:
        failed.append(f"recall@{TOP_K} below {RECALL_AT_3_FLOOR}")
    if mean_reciprocal_rank(standard) < MRR_FLOOR:
        failed.append(f"MRR below {MRR_FLOOR}")
    for message in failed:
        print(f"FAIL: {message}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
