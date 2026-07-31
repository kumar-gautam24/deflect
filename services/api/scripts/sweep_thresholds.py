"""Produces the deflection against wrong-answer curve used to pick the operating point.

No model calls. Retrieval runs once per item and is reused across every candidate
threshold, because the gate is a pure function of the scores it is given.
"""

import asyncio
from pathlib import Path

from deflect.answer.gate import GateThresholds, evaluate_gate
from deflect.db import SessionFactory
from deflect.evals.dataset import load_dataset
from deflect.retrieval.pipeline import RetrievalConfig, retrieve

DATASET = Path(__file__).parents[3] / "evals" / "golden.yaml"

# Cross-encoder scores are unbounded logits, not 0-1 similarities. Measured on this
# corpus, answerable questions score roughly +4 to +8 and unanswerable ones below 0.
CANDIDATES = [round(-8 + 0.5 * i, 2) for i in range(0, 33)]


async def main() -> None:
    items = load_dataset(DATASET)

    async with SessionFactory() as session:
        retrieved = {
            item.id: await retrieve(session, item.question, RetrievalConfig())
            for item in items
        }

    answerable = [i for i in items if not i.should_escalate]
    unanswerable = [i for i in items if i.should_escalate]

    print("| min_top_score | answered | wrongly refused | wrongly answered |")
    print("| --- | --- | --- | --- |")

    for threshold in CANDIDATES:
        thresholds = GateThresholds(min_top_score=threshold, min_margin=0.0)

        # grounded=True isolates the retrieval signal: this sweep is choosing the
        # score floor, and mixing in a model's self-report would confound it.
        answered = sum(
            not evaluate_gate(retrieved[i.id], True, thresholds).escalate for i in answerable
        )
        wrongly_answered = sum(
            not evaluate_gate(retrieved[i.id], True, thresholds).escalate for i in unanswerable
        )

        print(
            f"| {threshold:.2f} "
            f"| {answered / len(answerable):.2f} "
            f"| {(len(answerable) - answered) / len(answerable):.2f} "
            f"| {wrongly_answered / len(unanswerable):.2f} |"
        )

    print(f"\n{len(answerable)} answerable, {len(unanswerable)} unanswerable")


if __name__ == "__main__":
    asyncio.run(main())
