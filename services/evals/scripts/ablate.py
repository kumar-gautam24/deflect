"""Measures each retrieval stage independently. Output is pasted into the README.

No model calls: retrieval metrics are deterministic, which is what makes this table
cheap enough to re-run whenever the pipeline changes.
"""

import asyncio

import httpx
from _retrieval import DATASET, search

from evals.dataset import load_dataset
from evals.metrics import hit_at_k, mrr, precision_at_k

VARIANTS = {
    "dense only": {"use_lexical": False, "use_rerank": False},
    "lexical only": {"use_dense": False, "use_rerank": False},
    "hybrid": {"use_rerank": False},
    "hybrid + rerank": {},
}


async def main() -> None:
    items = [item for item in load_dataset(DATASET) if not item.should_escalate]

    print("| variant | hit@5 | MRR | precision@5 |")
    print("| --- | --- | --- | --- |")

    async with httpx.AsyncClient(timeout=60) as client:
        for name, overrides in VARIANTS.items():
            hits, ranks, precisions = [], [], []
            for item in items:
                results = await search(client, item.question, **overrides)
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
