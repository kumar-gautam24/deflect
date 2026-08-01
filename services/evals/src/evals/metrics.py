"""Retrieval metrics. Deterministic and LLM-free, so regressions are attributable."""


def _validate(k: int) -> None:
    if k < 1:
        raise ValueError("k must be at least 1")


def hit_at_k(retrieved: list[str], expected: list[str], k: int) -> float:
    _validate(k)
    return 1.0 if set(retrieved[:k]) & set(expected) else 0.0


def mrr(retrieved: list[str], expected: list[str]) -> float:
    wanted = set(expected)
    for rank, source in enumerate(retrieved, start=1):
        if source in wanted:
            return 1 / rank
    return 0.0


def precision_at_k(retrieved: list[str], expected: list[str], k: int) -> float:
    _validate(k)
    window = retrieved[:k]
    if not window:
        return 0.0
    # Counted over distinct sources: the same document retrieved twice is one
    # correct document, not two.
    return len(set(window) & set(expected)) / len(window)
