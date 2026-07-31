"""Measures each retrieval stage independently. Output is pasted into the README.

No model calls: retrieval metrics are deterministic, which is what makes this table
cheap enough to re-run whenever the pipeline changes.
"""

import asyncio
from pathlib import Path

from deflect.db import SessionFactory
from deflect.evals.dataset import load_dataset
from deflect.evals.metrics import hit_at_k, mrr, precision_at_k
from deflect.retrieval.pipeline import RetrievalConfig, retrieve

DATASET = Path(__file__).parents[3] / "evals" / "golden.yaml"

VARIANTS = {
    "dense only": RetrievalConfig(use_lexical=False, use_rerank=False),
    "lexical only": RetrievalConfig(use_dense=False, use_rerank=False),
    "hybrid": RetrievalConfig(use_rerank=False),
    "hybrid + rerank": RetrievalConfig(),
}


async def main() -> None:
    items = [item for item in load_dataset(DATASET) if not item.should_escalate]

    print("| variant | hit@5 | MRR | precision@5 |")
    print("| --- | --- | --- | --- |")

    async with SessionFactory() as session:
        for name, config in VARIANTS.items():
            hits, ranks, precisions = [], [], []
            for item in items:
                results = await retrieve(session, item.question, config)
                sources = [hit.source_path for hit in results]
                hits.append(hit_at_k(sources, item.expected_sources, k=5))
                ranks.append(mrr(sources, item.expected_sources))
                precisions.append(precision_at_k(sources, item.expected_sources, k=5))

            print(
                f"| {name} | {sum(hits) / len(hits):.3f} "
                f"| {sum(ranks) / len(ranks):.3f} "
                f"| {sum(precisions) / len(precisions):.3f} |"
            )

    print(f"\n{len(items)} answerable items")


if __name__ == "__main__":
    asyncio.run(main())
