"""Shows which score source can drive the confidence gate.

This is the measurement that justifies keeping the reranking stage. Reranking does
not improve retrieval metrics on this corpus, but Reciprocal Rank Fusion scores are
derived from rank position alone: the top-ranked chunk always scores 1/(k+1) no
matter how relevant it is. Without a cross-encoder there is no relevance signal to
threshold on, and therefore no escalation.
"""

import asyncio
import statistics
from pathlib import Path

from deflect.db import SessionFactory
from deflect.evals.dataset import load_dataset
from deflect.retrieval.pipeline import RetrievalConfig, retrieve

DATASET = Path(__file__).parents[3] / "evals" / "golden.yaml"


async def top_scores(session, items, config: RetrievalConfig) -> list[float]:
    scores = []
    for item in items:
        hits = await retrieve(session, item.question, config)
        scores.append(hits[0].score if hits else 0.0)
    return scores


async def main() -> None:
    items = load_dataset(DATASET)
    answerable = [i for i in items if not i.should_escalate]
    unanswerable = [i for i in items if i.should_escalate]

    variants = {
        "RRF fused (no rerank)": RetrievalConfig(use_rerank=False),
        "cross-encoder rerank": RetrievalConfig(),
    }

    print("| score source | answerable median | unanswerable median | separation |")
    print("| --- | --- | --- | --- |")

    async with SessionFactory() as session:
        for name, config in variants.items():
            answered = await top_scores(session, answerable, config)
            refused = await top_scores(session, unanswerable, config)
            separation = statistics.median(answered) - statistics.median(refused)
            print(
                f"| {name} | {statistics.median(answered):.4f} "
                f"| {statistics.median(refused):.4f} | {separation:.4f} |"
            )


if __name__ == "__main__":
    asyncio.run(main())
