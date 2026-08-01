"""Shows which score source can drive the confidence gate.

This is the measurement that justifies keeping the reranking stage. Reranking does not
improve retrieval metrics on this corpus, but Reciprocal Rank Fusion scores are derived
from rank position alone: the top-ranked chunk always scores 1/(k+1) no matter how
relevant it is. Without a cross-encoder there is no relevance signal to threshold on,
and therefore no escalation.
"""

import asyncio
import statistics

import httpx
from _retrieval import DATASET, search

from evals.dataset import load_dataset

VARIANTS = {"RRF fused (no rerank)": {"use_rerank": False}, "cross-encoder rerank": {}}


async def top_scores(client, items, overrides) -> list[float]:
    scores = []
    for item in items:
        hits = await search(client, item.question, **overrides)
        scores.append(hits[0].score if hits else 0.0)
    return scores


async def main() -> None:
    items = load_dataset(DATASET)
    answerable = [i for i in items if not i.should_escalate]
    unanswerable = [i for i in items if i.should_escalate]

    print("| score source | answerable median | unanswerable median | separation |")
    print("| --- | --- | --- | --- |")

    async with httpx.AsyncClient(timeout=60) as client:
        for name, overrides in VARIANTS.items():
            answered = await top_scores(client, answerable, overrides)
            refused = await top_scores(client, unanswerable, overrides)
            separation = statistics.median(answered) - statistics.median(refused)
            print(
                f"| {name} | {statistics.median(answered):.4f} "
                f"| {statistics.median(refused):.4f} | {separation:.4f} |"
            )


if __name__ == "__main__":
    asyncio.run(main())
