"""Loads and validates the hand-labeled golden dataset."""

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class GoldenItem:
    id: str
    question: str
    ideal_answer: str
    expected_sources: list[str]
    should_escalate: bool


def load_dataset(path: Path) -> list[GoldenItem]:
    raw = yaml.safe_load(path.read_text())

    items: list[GoldenItem] = []
    seen: set[str] = set()
    for entry in raw:
        item = GoldenItem(
            id=entry["id"],
            question=entry["question"],
            ideal_answer=entry["ideal_answer"],
            expected_sources=entry.get("expected_sources", []),
            should_escalate=entry["should_escalate"],
        )
        if item.id in seen:
            raise ValueError(f"duplicate item id: {item.id}")
        # Retrieval metrics are computed against expected_sources; an answerable item
        # without them would silently score zero and look like a retrieval regression.
        if not item.should_escalate and not item.expected_sources:
            raise ValueError(f"answerable item {item.id} has no expected_sources")
        if item.should_escalate and item.expected_sources:
            raise ValueError(
                f"item {item.id} expects escalation but names expected_sources"
            )
        seen.add(item.id)
        items.append(item)

    return items
